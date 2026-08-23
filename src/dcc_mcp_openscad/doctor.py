"""Adapter doctor compatibility layer pending the shared Core #2252/#2320 facade."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .__version__ import __version__
from .bridge import OpenscadCli, OpenScadError, OpenScadTimeoutError

SCHEMA_VERSION = "1.0"
MINIMUM_CORE_VERSION = "0.19.91"
MINIMUM_HOST_VERSION = "2021.01"
MINIMUM_HOST_TUPLE = (2021, 1)

EXIT_OK = 0
EXIT_PREFLIGHT = 10
EXIT_VERIFY = 40


@dataclass(frozen=True)
class DoctorRequest:
    operation: str
    executable: Optional[Path] = None
    timeout_secs: float = 20.0


def _core_tuple(value: object) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(value))
    return tuple(int(part) for part in match.groups()) if match else ()


def _host_tuple(value: object) -> tuple[int, ...]:
    match = re.search(r"(\d{4})\.(\d{1,2})(?:\.(\d+))?", str(value))
    if not match:
        return ()
    year, month, patch = match.groups()
    return (int(year), int(month), int(patch or 0))


def _core_version() -> Optional[str]:
    try:
        from dcc_mcp_core import __version__ as core_version

        return str(core_version)
    except (AttributeError, ImportError):
        return None


def _step(identifier: str, description: str, command: list[str], why: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "description": description,
        "command": command,
        "why": why,
    }


def _rerun_command(request: DoctorRequest, executable: Optional[str] = None) -> list[str]:
    command = ["dcc-mcp-openscad", request.operation]
    if executable:
        command.extend(["--executable", executable])
    command.append("--json")
    return command


def _result(
    request: DoctorRequest,
    *,
    exit_code: int,
    stage: str,
    reason: str,
    core_version: Optional[str],
    cli: Optional[OpenscadCli] = None,
    runtime: Optional[Mapping[str, Any]] = None,
    capabilities: Optional[Mapping[str, Any]] = None,
    next_steps: Optional[list[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    executable = cli.executable if cli is not None else None
    requested = str(request.executable) if request.executable is not None else None
    environment = os.environ.get("DCC_MCP_OPENSCAD_EXECUTABLE") or None
    if requested:
        source = "--executable"
    elif environment:
        source = "DCC_MCP_OPENSCAD_EXECUTABLE"
    elif executable:
        source = "PATH_or_standard_installation"
    else:
        source = None
    usable = exit_code == EXIT_OK
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": request.operation,
        "status": "ready" if usable else "failed",
        "exit_code": exit_code,
        "dcc_type": "openscad",
        "adapter_version": __version__,
        "core_version": core_version,
        "requirements": {
            "minimum_core_version": MINIMUM_CORE_VERSION,
            "minimum_host_version": MINIMUM_HOST_VERSION,
        },
        "configuration": {
            "allowed_roots": [str(path) for path in (cli.allowed_roots if cli else ())],
            "max_source_bytes": cli.max_source_bytes if cli else None,
            "max_timeout_secs": cli.max_timeout_secs if cli else None,
            "probe_timeout_secs": request.timeout_secs,
        },
        "discovery": {
            "requested": requested,
            "environment": environment,
            "executable": executable,
            "source": source,
            "launcher": Path(executable).suffix.lower() if executable else None,
            "endpoint_kind": "local_cli",
        },
        "runtime": dict(runtime or {}),
        "capabilities": dict(capabilities or {}),
        "verify": {
            "directly_usable": usable,
            "failure_stage": None if usable else stage,
            "failure_reason": None if usable else reason,
        },
        "steps": [],
        "next_steps": list(next_steps or []),
        "stage": stage,
        "reason": reason,
        "auto_provision": False,
        "cache": None,
        "binary_source": "os_package_manager_or_official_install",
    }


def run_doctor(request: DoctorRequest) -> dict[str, Any]:
    if request.operation not in {"doctor", "verify"}:
        raise ValueError("Unsupported OpenSCAD verification operation")
    core_version = _core_version()
    if core_version is None or _core_tuple(core_version) < _core_tuple(MINIMUM_CORE_VERSION):
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="core_version",
            reason="dcc-mcp-core 0.19.91+ is required",
            core_version=core_version,
            next_steps=[
                _step(
                    "upgrade-core",
                    "Install a supported DCC-MCP Core in this Python environment.",
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "dcc-mcp-core>=0.19.91,<1.0.0",
                    ],
                    "The standalone adapter imports and runs on Core 0.19.91 or newer.",
                )
            ],
        )
    try:
        configured = str(request.executable) if request.executable is not None else None
        if configured is None:
            cli = OpenscadCli.from_env()
        else:
            environment = OpenscadCli.from_env()
            cli = OpenscadCli(
                configured,
                allowed_roots=environment.allowed_roots,
                max_source_bytes=environment.max_source_bytes,
                max_timeout_secs=environment.max_timeout_secs,
            )
    except (OSError, TypeError, ValueError) as exc:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="configuration",
            reason="OpenSCAD runtime configuration is invalid: %s" % str(exc)[:500],
            core_version=core_version,
            next_steps=[
                _step(
                    "fix-configuration",
                    "Correct the OpenSCAD adapter environment and rerun doctor.",
                    _rerun_command(request),
                    "Numeric limits and workspace roots must be valid before CLI launch.",
                )
            ],
        )
    if not cli.executable:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="executable_discovery",
            reason="OpenSCAD CLI was not found",
            core_version=core_version,
            cli=cli,
            next_steps=[
                _step(
                    "install-or-configure-openscad",
                    "Install OpenSCAD through the OS or configure its executable, then rerun.",
                    _rerun_command(request),
                    "This adapter never downloads or caches OpenSCAD binaries.",
                )
            ],
        )
    missing_roots = [path for path in cli.allowed_roots if not path.is_dir()]
    if missing_roots:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="configuration",
            reason="Configured workspace root does not exist: %s" % missing_roots[0],
            core_version=core_version,
            cli=cli,
            next_steps=[
                _step(
                    "fix-workspace-roots",
                    "Create or correct the allowed workspace roots, then rerun doctor.",
                    _rerun_command(request, cli.executable),
                    "Source and output paths must stay inside existing allowed roots.",
                )
            ],
        )
    try:
        runtime = cli.status(timeout_secs=request.timeout_secs)
    except OpenScadTimeoutError:
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage="runtime_timeout",
            reason="OpenSCAD version detection timed out",
            core_version=core_version,
            cli=cli,
            next_steps=[
                _step(
                    "repair-runtime-and-retry",
                    "Repair the OpenSCAD CLI and rerun verification.",
                    _rerun_command(request, cli.executable),
                    "A discovered executable is not usable until its version probe completes.",
                )
            ],
        )
    except (OSError, OpenScadError) as exc:
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage="runtime_start",
            reason=str(exc)[:1000] or "OpenSCAD version detection failed",
            core_version=core_version,
            cli=cli,
            next_steps=[
                _step(
                    "repair-runtime-and-retry",
                    "Repair the OpenSCAD CLI and rerun verification.",
                    _rerun_command(request, cli.executable),
                    "The executable was found but its bounded version probe failed.",
                )
            ],
        )
    if runtime.get("ready") is not True:
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage="runtime_status",
            reason=str(runtime.get("reason") or "OpenSCAD reported that it is not ready")[:1000],
            core_version=core_version,
            cli=cli,
            runtime=runtime,
            next_steps=[
                _step(
                    "repair-runtime-and-retry",
                    "Repair the OpenSCAD CLI and rerun verification.",
                    _rerun_command(request, cli.executable),
                    "Direct usability requires an explicit ready status.",
                )
            ],
        )
    if _host_tuple(runtime.get("version")) < MINIMUM_HOST_TUPLE:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="host_version",
            reason="OpenSCAD 2021.01 or newer is required",
            core_version=core_version,
            cli=cli,
            runtime=runtime,
            next_steps=[
                _step(
                    "upgrade-openscad",
                    "Select OpenSCAD 2021.01+ and rerun verification.",
                    _rerun_command(request, cli.executable),
                    "The discovered OpenSCAD version is below the supported floor.",
                )
            ],
        )
    try:
        capabilities = cli.capabilities(status=runtime, timeout_secs=request.timeout_secs)
    except (OSError, OpenScadError) as exc:
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage="capability_probe",
            reason=str(exc)[:1000] or "OpenSCAD capability detection failed",
            core_version=core_version,
            cli=cli,
            runtime=runtime,
            next_steps=[
                _step(
                    "repair-runtime-and-retry",
                    "Repair the OpenSCAD CLI and rerun verification.",
                    _rerun_command(request, cli.executable),
                    "The version passed but the bounded help/capability probe failed.",
                )
            ],
        )
    if capabilities.get("ready") is not True:
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage="capability_probe",
            reason=str(capabilities.get("reason") or "OpenSCAD capabilities are not ready")[:1000],
            core_version=core_version,
            cli=cli,
            runtime=runtime,
            capabilities=capabilities,
            next_steps=[
                _step(
                    "repair-runtime-and-retry",
                    "Repair the OpenSCAD CLI and rerun verification.",
                    _rerun_command(request, cli.executable),
                    "Direct usability requires the version-specific capability probe.",
                )
            ],
        )
    return _result(
        request,
        exit_code=EXIT_OK,
        stage="verify",
        reason="OpenSCAD CLI is discovered and directly usable",
        core_version=core_version,
        cli=cli,
        runtime=runtime,
        capabilities=capabilities,
    )
