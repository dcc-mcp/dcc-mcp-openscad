from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _validate(payload: dict) -> None:
    from dcc_mcp_core.deployment import load_install_sop_schema
    from jsonschema import Draft202012Validator

    Draft202012Validator(load_install_sop_schema()).validate(payload)


def _fake_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str = "2026.09.29",
) -> Path:
    import dcc_mcp_openscad.doctor as lifecycle

    executable = tmp_path / ("openscad.com" if os.name == "nt" else "openscad")
    executable.write_bytes(b"official-openscad-doctor-fixture")

    def fake_run(command, *, timeout, cwd=None, env=None):
        del cwd, env
        assert 0 < timeout <= 20
        output = (
            "OpenSCAD version %s\n" % version
            if "--version" in command
            else "--hardwarnings --render -p -P\n"
        )
        return {
            "success": True,
            "returncode": 0,
            "stdout": output,
            "stderr": "",
            "truncated": False,
        }

    monkeypatch.setattr(lifecycle, "_run_bounded_command", fake_run)
    return executable


def test_doctor_cli_reports_missing_openscad_as_stable_json(tmp_path: Path, capsys) -> None:
    from dcc_mcp_openscad import server

    private_path = tmp_path / "private-token" / "missing-openscad"
    exit_code = server.main(["doctor", "--executable", str(private_path), "--json"])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    _validate(result)
    assert captured.err == ""
    assert exit_code == 10
    assert result["schema_version"] == 1
    assert result["command"] == "doctor"
    assert result["dcc_type"] == "openscad"
    assert result["verify"] == {
        "directly_usable": False,
        "failure_stage": "identity",
        "failure_reason": "file_missing",
    }
    from dcc_mcp_openscad.doctor import MINIMUM_CORE_VERSION, _version_tuple

    # Compared as a version tuple, not as a string: "0.100.0" < "0.20.36"
    # lexicographically, which would pass a floor it does not satisfy.
    assert _version_tuple(result["core_version"]) >= _version_tuple(MINIMUM_CORE_VERSION)
    assert result["receipt_path"] == "configured"
    assert str(private_path) not in captured.out


def test_doctor_reports_exact_version_capabilities_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from dcc_mcp_openscad import server

    executable = _fake_runtime(tmp_path, monkeypatch)
    exit_code = server.main(
        [
            "doctor",
            "--executable",
            str(executable),
            "--timeout-secs",
            "7",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    _validate(result)
    assert captured.err == ""
    assert exit_code == 0
    assert result["verify"]["directly_usable"] is True
    assert result["runtime"]["product"] == "OpenSCAD"
    assert result["runtime"]["version"] == "2026.09.29"
    assert result["runtime"]["capabilities"]["--hardwarnings"] is True
    assert result["runtime"]["capabilities"]["--render"] is True
    assert len(result["runtime"]["sha256"]) == 64
    assert str(executable) not in captured.out

    # The report states the host that was actually found and whether the
    # compatibility matrix covers it, so a caller can gate on `status`.
    host_matrix = result["runtime"]["host_matrix"]
    assert host_matrix["version"] == "2026.09.29"
    assert host_matrix["status"] == "supported"
    assert host_matrix["range"]["id"] == "2026.09.x"
    assert "2021.01.x" in host_matrix["supported_ranges"]


def test_doctor_enforces_current_core_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle
    from dcc_mcp_openscad import server

    executable = _fake_runtime(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle, "MINIMUM_CORE_VERSION", "999.0.0")

    exit_code = server.main(["doctor", "--executable", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    _validate(result)
    assert exit_code == 10
    assert result["verify"]["failure_stage"] == "core"
    assert result["verify"]["failure_reason"] == "core_version_unsupported"


def test_doctor_enforces_openscad_version_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from dcc_mcp_openscad import server

    executable = _fake_runtime(tmp_path, monkeypatch, version="2020.12")
    exit_code = server.main(["doctor", "--executable", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    _validate(result)
    assert exit_code == 10
    assert result["verify"]["failure_stage"] == "host_version"
    assert result["verify"]["failure_reason"] == "host_version_unsupported"


def test_doctor_rejects_a_host_outside_the_matrix_and_still_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Between the two verified lanes the host is unverified, not tolerated.

    A refusal that only carries an error code leaves the caller guessing which
    host was found, so the discovered version and verdict travel with it.
    """
    from dcc_mcp_openscad import server

    executable = _fake_runtime(tmp_path, monkeypatch, version="2024.01.15")
    exit_code = server.main(["doctor", "--executable", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    _validate(result)
    assert exit_code == 10
    assert result["error_code"] == "openscad_host_version_unlisted"
    assert result["verify"]["failure_stage"] == "host_version"

    host_matrix = result["runtime"]["host_matrix"]
    assert host_matrix["version"] == "2024.01.15"
    assert host_matrix["status"] == "unlisted"
    assert result["verify"]["directly_usable"] is False
    assert "2024.01.15" in " ".join(step["description"] for step in result["steps"])


def test_doctor_rejects_a_host_newer_than_the_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from dcc_mcp_openscad import server

    executable = _fake_runtime(tmp_path, monkeypatch, version="2099.01.01")
    exit_code = server.main(["doctor", "--executable", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    _validate(result)
    assert exit_code == 10
    assert result["error_code"] == "openscad_host_version_unverified"


def test_verify_requires_an_owned_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from dcc_mcp_openscad import server

    executable = _fake_runtime(tmp_path, monkeypatch)
    receipt = tmp_path / "missing-receipt.json"
    exit_code = server.main(
        [
            "verify",
            "--executable",
            str(executable),
            "--receipt-path",
            str(receipt),
            "--json",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    _validate(result)
    assert exit_code == 40
    assert result["verify"]["failure_stage"] == "receipt"
    assert result["verify"]["failure_reason"] == "receipt_missing"


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher preference")
def test_windows_explicit_exe_prefers_console_sibling(tmp_path: Path) -> None:
    from dcc_mcp_openscad.bridge import OpenscadCli

    gui = tmp_path / "openscad.exe"
    console = tmp_path / "openscad.com"
    gui.touch()
    console.touch()

    cli = OpenscadCli(str(gui), allowed_roots=[tmp_path])

    assert cli.executable == str(console.resolve())


@pytest.mark.parametrize(
    "version",
    [
        "2021.01",
        "2026.09.29",
        "2026.09.10",
        # Zero-padded days: snapshots built on the 1st-9th report them padded.
        # A host the version probe accepts must not then fail on its own
        # receipt, so the receipt-side parser accepts the same spelling.
        "2026.09.01",
        "2026.09.09",
        "2026.01.05",
    ],
)
def test_the_receipt_accepts_every_version_spelling_the_probe_emits(version: str) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    parsed = lifecycle._host_version_tuple(version)
    assert parsed, "the receipt parser rejected a version the probe emits: %s" % version
    assert parsed >= lifecycle.MINIMUM_HOST_TUPLE, version
    # The matrix verdict is a separate question from the parser: a covered
    # version is `supported`, anything else is refused with its own code.
    assert lifecycle.classify_host(version)["status"] in ("supported", "unlisted"), version


def test_a_receipt_for_a_zero_padded_snapshot_day_round_trips() -> None:
    """Regression: a padded day used to parse to () and fail as invalid."""
    import dcc_mcp_openscad.doctor as lifecycle

    for version in ("2026.09.01", "2026.09.09", "2026.01.05"):
        assert lifecycle._host_version_tuple(version) == (
            int(version[:4]),
            int(version[5:7]),
            int(version[8:]),
        ), version
    # A host below the matrix is still rejected by the same parser.
    assert lifecycle._host_version_tuple("2020.12") < lifecycle.MINIMUM_HOST_TUPLE
    assert lifecycle._host_version_tuple("garbage") == ()
