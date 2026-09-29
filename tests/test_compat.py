"""Tests for the machine-readable OpenSCAD host compatibility matrix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from dcc_mcp_openscad import compat

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def matrix() -> dict:
    return compat.load_matrix()


def test_matrix_declares_required_top_level_fields(matrix: dict) -> None:
    assert matrix["schema_version"] == 1
    assert matrix["host"] == "openscad"
    assert matrix["matrix_version"]
    assert matrix["supported_ranges"]


def test_every_supported_range_carries_its_own_evidence(matrix: dict) -> None:
    """A `supported` entry with no evidence is an unverified claim."""
    ranges = compat.supported_ranges(matrix)
    assert ranges, "the matrix declares no supported range"
    for entry in ranges:
        assert entry["status"] == "supported"
        assert entry.get("evidence_kind") in {"real_ci", "static"}, entry["id"]
        assert entry.get("evidence"), "range %s has no evidence text" % entry["id"]
        assert entry["min_version"] and entry["max_version"]


def test_real_ci_ranges_name_the_build_that_proves_them(matrix: dict) -> None:
    """Only a pinned build can back a real_ci claim."""
    for entry in compat.supported_ranges(matrix):
        if entry["evidence_kind"] == "real_ci":
            assert entry.get("ci_matrix_entry"), (
                "range %s claims real_ci but names no CI build" % entry["id"]
            )


def test_static_evidence_is_not_described_as_a_ci_run(matrix: dict) -> None:
    """The two grades of support must not read the same."""
    static = [e for e in compat.supported_ranges(matrix) if e["evidence_kind"] == "static"]
    for entry in static:
        assert entry.get("ci_matrix_entry") is None
        assert "static" in entry["evidence"].lower() or "contract" in entry["evidence"].lower()


def test_matrix_declares_two_distinct_evidence_grades(matrix: dict) -> None:
    kinds = {e["evidence_kind"] for e in compat.supported_ranges(matrix)}
    assert "real_ci" in kinds
    assert "static" in kinds


def test_declared_ranges_do_not_overlap(matrix: dict) -> None:
    spans = []
    for entry in compat.supported_ranges(matrix):
        low = compat.parse_version(entry["min_version"])
        high = compat.parse_version(entry["max_version"])
        assert low is not None and high is not None, entry["id"]
        assert low <= high, entry["id"]
        spans.append((low, high, entry["id"]))
    spans.sort()
    for (_, high, lower_id), (low, _, upper_id) in zip(spans, spans[1:]):
        assert high < low, "ranges %s and %s overlap" % (lower_id, upper_id)


@pytest.mark.parametrize(
    "version,expected_status,expected_kind",
    [
        ("2021.01", compat.SUPPORTED, "real_ci"),
        ("2021.01.7", compat.SUPPORTED, "real_ci"),
        ("2024.01.15", compat.SUPPORTED, "static"),
        ("2026.09.29", compat.SUPPORTED, "real_ci"),
        ("2020.12", compat.TOO_OLD, None),
        ("2019.05", compat.TOO_OLD, None),
        ("2026.10.01", compat.TOO_NEW, None),
        ("2027.01", compat.TOO_NEW, None),
        ("not-a-version", compat.UNKNOWN, None),
        ("", compat.UNKNOWN, None),
    ],
)
def test_classification(version: str, expected_status: str, expected_kind, matrix: dict) -> None:
    verdict = compat.classify_host(version, matrix)
    assert verdict["status"] == expected_status
    assert verdict["evidence_kind"] == expected_kind
    assert verdict["version"] == version
    # Only a supported host belongs to a range.
    assert (verdict["range"] is None) == (expected_status != compat.SUPPORTED)


def test_unsupported_reasons_name_the_covered_ranges(matrix: dict) -> None:
    for version in ("2020.12", "2027.01", "rubbish"):
        verdict = compat.classify_host(version, matrix)
        reason = compat.unsupported_reason(verdict)
        assert version in reason
        assert "2021.01" in reason
        assert reason != version


def test_is_supported_agrees_with_classify_host(matrix: dict) -> None:
    for version in ("2021.01", "2024.01.15", "2026.09.29", "2020.12", "2027.01"):
        verdict = compat.classify_host(version, matrix)
        assert compat.is_supported(version, matrix) is (verdict["status"] == compat.SUPPORTED)


def test_parse_version_normalises_release_and_snapshot_stamps() -> None:
    assert compat.parse_version("2021.01") == (2021, 1, 0)
    assert compat.parse_version("2021.01.7") == (2021, 1, 7)
    assert compat.parse_version("2026.09.29") == (2026, 9, 29)
    assert compat.parse_version("2026.9.29") == (2026, 9, 29)
    # A trailing build suffix is tolerated, a bad month is not.
    assert compat.parse_version("2026.09.29.ai1234") == (2026, 9, 29)
    assert compat.parse_version("2026.13.01") is None
    assert compat.parse_version("garbage") is None


def test_breaking_changes_apply_from_their_declared_version(matrix: dict) -> None:
    """The 2021.01 release line predates every declared change."""
    assert compat.breaking_changes_for("2021.01", matrix) == []
    later = compat.breaking_changes_for("2026.09.29", matrix)
    assert {entry["id"] for entry in later} >= {"view-flags-removed"}
    for entry in later:
        assert entry.get("remediation")


def test_every_real_ci_tier_is_actually_run_in_ci(matrix: dict) -> None:
    """A real_ci claim with no matching CI entry is a false claim."""
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    job = workflow["jobs"].get("openscad-real")
    assert job, "no openscad-real job; real_ci claims would be unbacked"
    entries = {
        str(include.get("openscad_asset")) for include in job["strategy"]["matrix"]["include"]
    }
    for entry in compat.supported_ranges(matrix):
        if entry["evidence_kind"] == "real_ci":
            assert entry["ci_matrix_entry"] in entries, (
                "range %s claims real_ci via %s but CI never runs it"
                % (entry["id"], entry["ci_matrix_entry"])
            )
            # The claim must also say which version that build produced.
            assert entry["id"] in entry["evidence"] or "Real OpenSCAD" in entry["evidence"]


def test_matrix_is_valid_json_and_ships_with_the_package() -> None:
    """The matrix is read at runtime, so it must be in the wheel."""
    shipped = Path(compat.__file__).with_name("compat_matrix.json")
    assert shipped.is_file()
    assert json.loads(shipped.read_text(encoding="utf-8"))["host"] == "openscad"
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "compat_matrix.json" in pyproject
