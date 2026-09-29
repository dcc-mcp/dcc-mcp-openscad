"""Machine-readable OpenSCAD host compatibility matrix.

The matrix is the single source of truth for "is this host supported". The
failure it prevents is the silent one: an undeclared OpenSCAD version being
treated as good enough because nothing rejected it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcc_mcp_openscad import compat

MATRIX = compat.load_matrix()


def test_every_supported_range_is_well_formed_and_ordered():
    ranges = compat.supported_ranges()

    assert ranges, "a matrix with no supported range rejects every host"
    for entry in ranges:
        minimum = compat.parse_version(str(entry["min_version"]))
        maximum = compat.parse_version(str(entry["max_version"]))
        assert minimum is not None, entry
        assert maximum is not None, entry
        assert minimum <= maximum, entry
        # Evidence is what makes a range a claim instead of a guess.
        assert str(entry.get("evidence") or "").strip(), entry
        assert str(entry.get("ci_matrix_entry") or "").strip(), entry


def test_the_declared_ranges_do_not_overlap():
    ranges = sorted(
        (
            compat.parse_version(str(entry["min_version"])),
            compat.parse_version(entry["max_version"]),
        )  # type: ignore[arg-type]
        for entry in compat.supported_ranges()
    )

    for (_low, high), (next_low, _next_high) in zip(ranges, ranges[1:]):
        assert high < next_low, "overlapping ranges make a verdict ambiguous"


def test_parse_version_understands_releases_and_dated_snapshots():
    assert compat.parse_version("2021.01") == (2021, 1, 0)
    assert compat.parse_version("2026.09.29") == (2026, 9, 29)
    assert compat.parse_version("2021.01.05") == (2021, 1, 5)
    assert compat.parse_version("not a version") is None
    assert compat.parse_version("") is None


def test_format_host_version_matches_openscad_spelling():
    assert compat.format_host_version((2021, 1, 0)) == "2021.01"
    assert compat.format_host_version((2026, 9, 29)) == "2026.09.29"


def test_supported_ranges_are_labelled_for_display():
    labels = compat.supported_range_labels()

    assert labels == sorted(labels)
    assert all(label.endswith(".x") for label in labels)
    assert len(labels) == len(MATRIX["supported_ranges"])


def test_minimum_host_version_is_the_lowest_declared_minimum():
    minimums = [
        compat.parse_version(str(entry["min_version"])) for entry in compat.supported_ranges()
    ]

    assert compat.parse_version(compat.minimum_host_version()) == min(minimums)


@pytest.mark.parametrize(
    "version,expected",
    [
        ("2021.01", compat.SUPPORTED),
        ("2021.01.99", compat.SUPPORTED),
        ("2026.09.29", compat.SUPPORTED),
        ("2026.09.01", compat.SUPPORTED),
        ("2020.12", compat.TOO_OLD),
        ("2019.05", compat.TOO_OLD),
        ("2024.01.15", compat.UNLISTED),
        ("2027.01.01", compat.TOO_NEW),
        ("garbage", compat.UNKNOWN),
        ("", compat.UNKNOWN),
    ],
)
def test_classify_host_places_each_version(version, expected):
    verdict = compat.classify_host(version)

    assert verdict["status"] == expected, version
    assert verdict["version"] == version
    assert verdict["matrix_version"] == MATRIX["matrix_version"]
    assert verdict["supported_ranges"] == compat.supported_range_labels()


def test_a_supported_version_reports_the_range_and_its_evidence():
    verdict = compat.classify_host("2021.01")

    assert verdict["range"] is not None
    assert verdict["range"]["id"] == "2021.01.x"
    assert verdict["range"]["evidence"]
    assert compat.is_supported("2021.01") is True


def test_an_unsupported_version_has_no_range_and_names_the_covered_ones():
    verdict = compat.classify_host("2024.01.15")

    assert verdict["range"] is None
    assert compat.is_supported("2024.01.15") is False
    reason = compat.unsupported_reason(verdict)
    assert "2024.01.15" in reason
    for label in compat.supported_range_labels():
        assert label in reason


def test_breaking_changes_only_apply_from_their_declared_version():
    declared = MATRIX["breaking_changes"]
    assert declared, "the matrix should record what it knows about host drift"

    applied = {entry["id"] for entry in compat.breaking_changes_for("2026.09.29")}
    assert applied == {entry["id"] for entry in declared}

    older = {entry["id"] for entry in compat.breaking_changes_for("2021.01")}
    assert older < applied, "a change cannot apply to a host released before it"
    assert compat.breaking_changes_for("garbage") == []


def test_the_matrix_ships_inside_the_package():
    """The matrix is read at runtime, so it has to travel with the wheel."""
    assert Path(compat.MATRIX_PATH).is_file()
    assert MATRIX["host"] == "openscad"
    assert str(MATRIX.get("matrix_version") or "").strip()


# --------------------------------------------------------------------------
# Real host: the matrix is checked against the OpenSCAD that CI installed
# --------------------------------------------------------------------------


def _real_openscad() -> str:
    return os.environ.get("OPENSCAD_TEST_EXECUTABLE", "")


@pytest.mark.openscad
@pytest.mark.skipif(not _real_openscad(), reason="OPENSCAD_TEST_EXECUTABLE is not set")
def test_real_openscad_version_is_covered_by_the_compatibility_matrix(tmp_path: Path):
    """Runs on every real CI leg as matrix evidence."""
    from dcc_mcp_openscad.bridge import OpenscadCli

    status = OpenscadCli(_real_openscad(), allowed_roots=[tmp_path]).status()

    version = status["version"]
    host_matrix = status["host_matrix"]

    assert host_matrix["status"] == compat.SUPPORTED, (
        "OpenSCAD %s is not covered by the compatibility matrix; add a verified "
        "range for it instead of running unverified" % version
    )
    assert host_matrix["range"]["id"] in host_matrix["supported_ranges"]
    assert status["ready"] is True
