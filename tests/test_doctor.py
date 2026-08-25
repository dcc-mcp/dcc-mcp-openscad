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
    version: str = "2024.01.15",
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
    assert result["core_version"] >= "0.20.14"
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
    assert result["runtime"]["version"] == "2024.01.15"
    assert result["runtime"]["capabilities"]["--hardwarnings"] is True
    assert result["runtime"]["capabilities"]["--render"] is True
    assert len(result["runtime"]["sha256"]) == 64
    assert str(executable) not in captured.out


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
