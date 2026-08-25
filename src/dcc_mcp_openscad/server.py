from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from dcc_mcp_core import DccServerOptions
from dcc_mcp_core.server_base import DccServerBase

from .__version__ import __version__

_server: Optional["OpenscadMcpServer"] = None


class _ArgumentFailure(RuntimeError):
    pass


class _ParserCompleted(RuntimeError):
    pass


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _ArgumentFailure("invalid arguments")

    def exit(self, status: int = 0, message: Optional[str] = None) -> None:
        del message
        if status == 0:
            raise _ParserCompleted
        raise _ArgumentFailure("argument parser exited")


class OpenscadMcpServer(DccServerBase):
    def __init__(self, port: Optional[int] = None):
        options = DccServerOptions.from_env(
            "openscad",
            Path(__file__).parent / "skills",
            port=port,
            server_name="dcc-mcp-openscad",
            server_version=__version__,
            adapter_version=__version__,
            instance_type="standalone",
        )
        super().__init__(options=options)

    def _version_string(self):
        return __version__


def start_server(port: Optional[int] = None):
    global _server
    if _server is None or not _server.is_running:
        _server = OpenscadMcpServer(port)
        _server.register_builtin_actions()
        _server.start()
    return _server


def stop_server():
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def _build_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(description="Run or manage the OpenSCAD adapter.")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_SafeArgumentParser
    )
    for operation in ("doctor", "install", "status", "verify", "uninstall", "upgrade"):
        command = subparsers.add_parser(
            operation,
            help="Run the %s lifecycle operation." % operation,
        )
        command.add_argument("--json", action="store_true", dest="json_output")
        command.add_argument("--executable", "--openscad", type=Path)
        command.add_argument("--timeout-secs", type=float, default=20.0)
        command.add_argument("--receipt-path", type=Path)
        command.add_argument("--yes", action="store_true")
        command.add_argument("--dry-run", action="store_true")
    return parser


def _print_doctor_result(result: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, sort_keys=True))
        return
    print("%s: %s" % (result.get("status", "unknown"), result.get("reason", "")))
    for step in result.get("next_steps", []):
        command = step.get("command") if isinstance(step, Mapping) else None
        if isinstance(command, list):
            print("next: %s" % " ".join(str(part) for part in command))


def _run_server() -> None:
    event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: event.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: event.set())
    start_server()
    try:
        event.wait()
    finally:
        stop_server()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the legacy no-argument service or a standalone verification command."""
    started = time.monotonic()
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        _run_server()
        return 0
    from .doctor import DoctorRequest, run_doctor

    try:
        args = _build_parser().parse_args(arguments)
    except _ParserCompleted:
        return 0
    except _ArgumentFailure:
        operation = arguments[0] if arguments and not arguments[0].startswith("-") else "invalid"
        result = run_doctor(DoctorRequest(operation=operation, timeout_secs=-1))
        _print_doctor_result(result, json_output="--json" in arguments)
        return int(result["exit_code"])

    result = run_doctor(
        DoctorRequest(
            operation=args.command,
            executable=args.executable,
            timeout_secs=args.timeout_secs,
            receipt_path=args.receipt_path,
            yes=args.yes,
            dry_run=args.dry_run,
            deadline=started + args.timeout_secs,
        )
    )
    _print_doctor_result(result, json_output=args.json_output)
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
