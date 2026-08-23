from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def test_doctor_cli_reports_missing_openscad_as_stable_json(tmp_path: Path, capsys) -> None:
    from dcc_mcp_openscad import server

    exit_code = server.main(
        [
            "doctor",
            "--executable",
            str(tmp_path / "missing-openscad"),
            "--json",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 10
    assert result["schema_version"] == "1.0"
    assert result["operation"] == "doctor"
    assert result["dcc_type"] == "openscad"
    assert result["verify"] == {
        "directly_usable": False,
        "failure_stage": "executable_discovery",
        "failure_reason": "OpenSCAD CLI was not found",
    }
    assert result["discovery"]["executable"] is None
    assert result["requirements"]["minimum_core_version"] == "0.19.91"
    assert result["requirements"]["minimum_host_version"] == "2021.01"
    assert result["auto_provision"] is False
    assert result["cache"] is None
    assert "receipt_path" not in result
    assert isinstance(result["next_steps"][0]["command"], list)


def test_verify_reports_version_capabilities_and_configuration(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as doctor
    from dcc_mcp_openscad import server
    from dcc_mcp_openscad.bridge import OpenscadCli

    executable = tmp_path / "openscad"
    executable.touch()
    monkeypatch.setattr(doctor, "_core_version", lambda: "0.19.91")
    monkeypatch.setattr(
        OpenscadCli,
        "status",
        lambda _self, timeout_secs: {
            "ready": True,
            "version": "2024.01.15",
            "instance_type": "standalone",
            "probe_timeout_secs": timeout_secs,
        },
    )
    monkeypatch.setattr(
        OpenscadCli,
        "capabilities",
        lambda _self, status, timeout_secs: {
            "ready": True,
            "flags": {"--hardwarnings": True, "--render": True},
            "output_extensions": [".stl", ".png"],
        },
    )

    exit_code = server.main(
        [
            "verify",
            "--executable",
            str(executable),
            "--timeout-secs",
            "7",
            "--json",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["verify"]["directly_usable"] is True
    assert result["runtime"]["version"] == "2024.01.15"
    assert result["capabilities"]["flags"]["--hardwarnings"] is True
    assert result["configuration"]["probe_timeout_secs"] == 7.0
    assert result["discovery"]["executable"] == str(executable.resolve())
    assert result["discovery"]["endpoint_kind"] == "local_cli"


@pytest.mark.parametrize(
    ("core_version", "host_version", "expected_stage"),
    [("0.19.90", None, "core_version"), ("0.19.91", "2020.12", "host_version")],
)
def test_doctor_enforces_core_and_openscad_version_floors(
    core_version: str,
    host_version: str | None,
    expected_stage: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    import dcc_mcp_openscad.doctor as doctor
    from dcc_mcp_openscad import server
    from dcc_mcp_openscad.bridge import OpenscadCli

    executable = tmp_path / "openscad"
    executable.touch()
    monkeypatch.setattr(doctor, "_core_version", lambda: core_version)
    if host_version is not None:
        monkeypatch.setattr(
            OpenscadCli,
            "status",
            lambda _self, timeout_secs: {
                "ready": True,
                "version": host_version,
                "instance_type": "standalone",
            },
        )

    exit_code = server.main(["doctor", "--executable", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 10
    assert result["verify"]["failure_stage"] == expected_stage


def test_verify_requires_explicit_runtime_and_capability_readiness(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as doctor
    from dcc_mcp_openscad import server
    from dcc_mcp_openscad.bridge import OpenscadCli

    executable = tmp_path / "openscad"
    executable.touch()
    monkeypatch.setattr(doctor, "_core_version", lambda: "0.19.91")
    monkeypatch.setattr(
        OpenscadCli,
        "status",
        lambda _self, timeout_secs: {
            "ready": False,
            "reason": "openscad_version_failed",
            "version": "2024.01",
        },
    )
    monkeypatch.setattr(
        OpenscadCli,
        "capabilities",
        lambda _self, status, timeout_secs: pytest.fail("capabilities must not run"),
    )

    exit_code = server.main(["verify", "--executable", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 40
    assert result["verify"]["failure_stage"] == "runtime_status"


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher preference")
def test_windows_explicit_exe_prefers_console_sibling(tmp_path: Path) -> None:
    from dcc_mcp_openscad.bridge import OpenscadCli

    gui = tmp_path / "openscad.exe"
    console = tmp_path / "openscad.com"
    gui.touch()
    console.touch()

    cli = OpenscadCli(str(gui), allowed_roots=[tmp_path])

    assert cli.executable == str(console.resolve())
