"""Agent-first OpenSCAD Install SOP lifecycle using the official Core contract."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

try:
    from importlib import metadata as importlib_metadata
except ImportError:  # pragma: no cover - exercised by native Python 3.7 CI
    import importlib_metadata

import dcc_mcp_core
from dcc_mcp_core.deployment import (
    INSTALL_EXIT_ACQUIRE,
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_REQUIRES_RESTART,
    INSTALL_EXIT_VERIFY,
    INSTALL_SOP_SCHEMA_VERSION,
    load_install_sop_schema,
)

from .__version__ import __version__
from ._process import ProcessCleanupError
from ._process import run_bounded_command as _run_bounded_command

SCHEMA_VERSION = INSTALL_SOP_SCHEMA_VERSION
MINIMUM_CORE_VERSION = "0.20.14"
MINIMUM_HOST_VERSION = "2021.01"
MINIMUM_HOST_TUPLE = (2021, 1, 0)
DCC_TYPE = "openscad"
RECEIPT_VERSION = 1
LIFECYCLE_COMMANDS = frozenset({"doctor", "install", "status", "verify", "uninstall", "upgrade"})

EXIT_OK = INSTALL_EXIT_OK
EXIT_PREFLIGHT = INSTALL_EXIT_PREFLIGHT
EXIT_ACQUIRE = INSTALL_EXIT_ACQUIRE
EXIT_INSTALL = INSTALL_EXIT_INSTALL
EXIT_VERIFY = INSTALL_EXIT_VERIFY
EXIT_REQUIRES_RESTART = INSTALL_EXIT_REQUIRES_RESTART

_VERSION = re.compile(
    r"^OpenSCAD(?:\s+Version:|\s+version)?\s*:?\s*"
    r"((?:0|[1-9][0-9]{3})\.(?:0[1-9]|1[0-2]|[1-9])(?:\.(?:0|[1-9][0-9]*))?)\s*$",
    re.IGNORECASE,
)
_SEMVER = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_POINT = 0x400
_DEFAULT_RECEIPT = Path.home() / ".dcc-mcp" / "receipts" / "openscad.json"
_CAPABILITY_FLAGS = (
    "--hardwarnings",
    "--check-parameters",
    "--check-parameter-ranges",
    "--render",
    "-p",
    "-P",
)


class LifecycleFailure(RuntimeError):
    """Stable classified failure that never carries a raw external diagnostic."""

    def __init__(self, exit_code: int, stage: str, reason: str) -> None:
        super().__init__(reason)
        self.exit_code = int(exit_code)
        self.stage = stage
        self.reason = reason


@dataclass(frozen=True)
class DoctorRequest:
    operation: str
    executable: Optional[Path] = None
    timeout_secs: float = 20.0
    receipt_path: Optional[Path] = None
    yes: bool = False
    dry_run: bool = False
    deadline: Optional[float] = None


LifecycleRequest = DoctorRequest


def _version_tuple(value: object) -> tuple[int, int, int]:
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?", str(value))
    if match is None:
        return ()  # type: ignore[return-value]
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _host_version_tuple(value: object) -> tuple[int, int, int]:
    match = re.fullmatch(r"([0-9]{4})\.(0[1-9]|1[0-2]|[1-9])(?:\.(0|[1-9][0-9]*))?", str(value))
    if match is None:
        return ()  # type: ignore[return-value]
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise LifecycleFailure(EXIT_VERIFY, "deadline", "operation_timeout")
    return value


def _receipt_path(value: Optional[Path]) -> Path:
    configured = value or Path(os.environ.get("DCC_MCP_OPENSCAD_RECEIPT", str(_DEFAULT_RECEIPT)))
    return configured.expanduser().absolute()


def _is_reparse_or_symlink(path: Path, metadata: os.stat_result) -> bool:
    del path
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_POINT)


def _assert_no_reparse_ancestors(path: Path) -> None:
    candidate = Path(path).expanduser().absolute().parent
    ancestors = [candidate, *candidate.parents]
    for ancestor in reversed(ancestors):
        try:
            metadata = ancestor.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise LifecycleFailure(EXIT_PREFLIGHT, "identity", "path_observation_failed") from exc
        if _is_reparse_or_symlink(ancestor, metadata):
            raise LifecycleFailure(EXIT_PREFLIGHT, "identity", "reparse_or_symlink_rejected")
        if not stat.S_ISDIR(metadata.st_mode):
            raise LifecycleFailure(EXIT_PREFLIGHT, "identity", "path_parent_not_directory")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _capture_file_identity(path: Path) -> Dict[str, Any]:
    candidate = Path(path).expanduser().absolute()
    _assert_no_reparse_ancestors(candidate)
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "identity", "file_missing") from exc
    if _is_reparse_or_symlink(candidate, before):
        raise LifecycleFailure(EXIT_PREFLIGHT, "identity", "reparse_or_symlink_rejected")
    if not stat.S_ISREG(before.st_mode):
        raise LifecycleFailure(EXIT_PREFLIGHT, "identity", "file_not_regular")
    try:
        digest = _sha256(candidate)
        after = candidate.lstat()
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "identity", "file_observation_failed") from exc
    before_key = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(getattr(before, "st_mtime_ns", int(before.st_mtime * 1_000_000_000))),
    )
    after_key = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(getattr(after, "st_mtime_ns", int(after.st_mtime * 1_000_000_000))),
    )
    if before_key != after_key or _is_reparse_or_symlink(candidate, after):
        raise LifecycleFailure(EXIT_VERIFY, "identity", "file_identity_changed")
    return {
        "path": str(resolved),
        "size": int(after.st_size),
        "sha256": digest,
        "device": int(after.st_dev),
        "inode": int(after.st_ino),
        "mtime_ns": after_key[3],
    }


def _identity_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(
        value.get(name) for name in ("path", "size", "sha256", "device", "inode", "mtime_ns")
    )


def _same_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return _identity_key(left) == _identity_key(right)


def _resolve_executable(explicit: Optional[Path]) -> Path:
    candidates = []
    if explicit is not None:
        candidates.append(explicit)
    else:
        configured = os.environ.get("DCC_MCP_OPENSCAD_EXECUTABLE")
        if configured:
            candidates.append(Path(configured))
        for name in ("openscad.com", "openscad"):
            discovered = shutil.which(name)
            if discovered:
                candidates.append(Path(discovered))
        if os.name == "nt":
            root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "OpenSCAD"
            candidates.extend((root / "openscad.com", root / "openscad.exe"))
    for candidate in candidates:
        lexical = candidate.expanduser().absolute()
        if lexical.is_dir():
            lexical = lexical / ("openscad.com" if os.name == "nt" else "openscad")
        try:
            _capture_file_identity(lexical)
        except LifecycleFailure:
            if explicit is not None:
                raise
            continue
        return lexical
    raise LifecycleFailure(EXIT_PREFLIGHT, "executable_discovery", "openscad_not_found")


def _probe_environment() -> Dict[str, str]:
    allowed = {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
    }
    return {name: value for name, value in os.environ.items() if name.upper() in allowed}


def _probe_output(result: Mapping[str, Any], reason: str) -> str:
    if not result.get("success") or result.get("returncode") != 0 or result.get("truncated"):
        failure = str(result.get("reason") or reason)
        if failure == "probe_timeout":
            raise LifecycleFailure(EXIT_VERIFY, "runtime", "runtime_timeout")
        if failure == "probe_cleanup_failed":
            raise LifecycleFailure(EXIT_VERIFY, "cleanup", "runtime_cleanup_failed")
        raise LifecycleFailure(EXIT_VERIFY, "runtime", reason)
    return (str(result.get("stdout") or "") + "\n" + str(result.get("stderr") or "")).strip()


def _run_probe(command: list[str], *, timeout: float, env: Mapping[str, str]) -> Dict[str, Any]:
    try:
        return _run_bounded_command(command, timeout=timeout, env=env)
    except ProcessCleanupError:
        raise LifecycleFailure(EXIT_VERIFY, "cleanup", "runtime_cleanup_failed") from None


def _capture_runtime(executable: Path, deadline: float) -> Dict[str, Any]:
    before = _capture_file_identity(executable)
    version_result = _run_probe(
        [before["path"], "--version"],
        timeout=_remaining(deadline),
        env=_probe_environment(),
    )
    version_text = _probe_output(version_result, "version_probe_failed")
    after_version = _capture_file_identity(executable)
    if not _same_identity(before, after_version):
        raise LifecycleFailure(EXIT_VERIFY, "identity", "runtime_identity_changed")
    lines = [line.strip() for line in version_text.splitlines() if line.strip()]
    match = _VERSION.fullmatch(lines[0]) if len(lines) == 1 else None
    if match is None:
        raise LifecycleFailure(EXIT_VERIFY, "runtime", "product_identity_invalid")
    version = match.group(1)
    if _host_version_tuple(version) < MINIMUM_HOST_TUPLE:
        raise LifecycleFailure(EXIT_PREFLIGHT, "host_version", "host_version_unsupported")
    help_result = _run_probe(
        [before["path"], "--help"],
        timeout=_remaining(deadline),
        env=_probe_environment(),
    )
    help_text = _probe_output(help_result, "capability_probe_failed")
    after_help = _capture_file_identity(executable)
    if not _same_identity(before, after_help):
        raise LifecycleFailure(EXIT_VERIFY, "identity", "runtime_identity_changed")
    return {
        **before,
        "product": "OpenSCAD",
        "version": version,
        "capabilities": {flag: flag in help_text for flag in _CAPABILITY_FLAGS},
    }


def _distribution_root(distribution: importlib_metadata.Distribution) -> Path:
    root = Path(distribution.locate_file("")).resolve(strict=True)
    if not root.is_dir():
        raise LifecycleFailure(EXIT_PREFLIGHT, "core", "core_distribution_invalid")
    return root


def _capture_core_identity() -> Dict[str, Any]:
    try:
        distribution = importlib_metadata.distribution("dcc-mcp-core")
        version = distribution.version
        root = _distribution_root(distribution)
        module = Path(str(dcc_mcp_core.__file__)).resolve(strict=True)
        module.relative_to(root)
    except (ImportError, importlib_metadata.PackageNotFoundError, OSError, ValueError) as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "core", "core_distribution_invalid") from exc
    if _version_tuple(version) < _version_tuple(MINIMUM_CORE_VERSION):
        raise LifecycleFailure(EXIT_PREFLIGHT, "core", "core_version_unsupported")
    try:
        python_executable = Path(sys.executable).resolve(strict=True)
    except OSError as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "core", "python_executable_invalid") from exc
    python_identity = _capture_file_identity(python_executable)
    module_identity = _capture_file_identity(module)
    return {"version": version, "module": module_identity, "python": python_identity}


def _validate_schema_loader() -> None:
    try:
        schema = load_install_sop_schema()
    except (OSError, ValueError, TypeError) as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "schema", "core_schema_unavailable") from exc
    if (
        not isinstance(schema, dict)
        or schema.get("type") != "object"
        or schema.get("properties", {}).get("schema_version", {}).get("const") != SCHEMA_VERSION
        or not isinstance(schema.get("required"), list)
    ):
        raise LifecycleFailure(EXIT_PREFLIGHT, "schema", "core_schema_invalid")


def _validate_identity_record(
    value: object, label: str, extra_fields: tuple[str, ...] = ()
) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_%s_invalid" % label)
    required = {"path", "size", "sha256", "device", "inode", "mtime_ns"}
    if set(value) != required | set(extra_fields):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_%s_invalid" % label)
    if not isinstance(value["path"], str) or not Path(value["path"]).is_absolute():
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_%s_invalid" % label)
    if not isinstance(value["size"], int) or value["size"] < 0:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_%s_invalid" % label)
    if not isinstance(value["sha256"], str) or _SHA256.fullmatch(value["sha256"]) is None:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_%s_invalid" % label)
    for key in ("device", "inode", "mtime_ns"):
        if not isinstance(value[key], int):
            raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_%s_invalid" % label)
    return dict(value)


def _read_receipt(path: Path) -> Optional[Dict[str, Any]]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_unavailable") from exc
    if _is_reparse_or_symlink(path, metadata):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_reparse_rejected")
    identity_before = _capture_file_identity(path)
    if identity_before["size"] > 256 * 1024:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_too_large")
    try:
        serialized = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_invalid") from exc
    identity_after = _capture_file_identity(path)
    if not _same_identity(identity_before, identity_after):
        raise LifecycleFailure(EXIT_VERIFY, "receipt", "receipt_identity_changed")
    try:
        value = json.loads(serialized)
    except ValueError as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_invalid") from exc
    if not isinstance(value, dict) or set(value) != {
        "receipt_version",
        "dcc_type",
        "adapter_version",
        "core",
        "python",
        "openscad",
    }:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_invalid")
    if value.get("receipt_version") != RECEIPT_VERSION or value.get("dcc_type") != DCC_TYPE:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_owner_mismatch")
    if (
        not isinstance(value.get("adapter_version"), str)
        or _SEMVER.fullmatch(value["adapter_version"]) is None
    ):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_adapter_version_invalid")
    core = value.get("core")
    if not isinstance(core, dict) or set(core) != {"version", "module"}:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_core_invalid")
    if not isinstance(core.get("version"), str) or _version_tuple(core["version"]) < _version_tuple(
        MINIMUM_CORE_VERSION
    ):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_core_invalid")
    core["module"] = _validate_identity_record(core.get("module"), "core")
    value["python"] = _validate_identity_record(value.get("python"), "python")
    openscad = _validate_identity_record(
        value.get("openscad"), "openscad", ("product", "version", "capabilities")
    )
    if (
        openscad.get("product") != "OpenSCAD"
        or not isinstance(openscad.get("version"), str)
        or _host_version_tuple(openscad["version"]) < MINIMUM_HOST_TUPLE
    ):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_openscad_invalid")
    capabilities = openscad.get("capabilities")
    if (
        not isinstance(capabilities, dict)
        or set(capabilities) != set(_CAPABILITY_FLAGS)
        or any(not isinstance(value, bool) for value in capabilities.values())
    ):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_openscad_invalid")
    value["openscad"] = openscad
    return value


def _receipt_payload(core: Mapping[str, Any], runtime: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "receipt_version": RECEIPT_VERSION,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core": {"version": core["version"], "module": dict(core["module"])},
        "python": dict(core["python"]),
        "openscad": dict(runtime),
    }


def _replace_path(source: Path, destination: Path) -> None:
    os.replace(str(source), str(destination))


def _assert_safe_receipt_parent(path: Path) -> None:
    parent = path.parent
    _assert_no_reparse_ancestors(path)
    parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_ancestors(path)
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise LifecycleFailure(EXIT_INSTALL, "receipt", "receipt_parent_unavailable") from exc
    if _is_reparse_or_symlink(parent, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_parent_unsafe")
    if path.exists():
        _capture_file_identity(path)


def _write_receipt_atomic(path: Path, payload: Mapping[str, Any], deadline: float) -> None:
    _assert_safe_receipt_parent(path)
    temporary = path.parent / (".%s.stage-%s" % (path.name, uuid.uuid4().hex))
    backup = path.parent / (".%s.backup-%s" % (path.name, uuid.uuid4().hex))
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    prior_displaced = False
    new_committed = False

    def rollback() -> None:
        nonlocal new_committed, prior_displaced
        try:
            if prior_displaced:
                if not backup.is_file():
                    raise OSError("receipt backup is unavailable")
                _replace_path(backup, path)
                prior_displaced = False
                new_committed = False
            elif new_committed and path.exists():
                path.unlink()
                new_committed = False
        except OSError:
            raise LifecycleFailure(EXIT_INSTALL, "rollback", "receipt_rollback_failed") from None

    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _remaining(deadline)
        if path.exists():
            _capture_file_identity(path)
            _replace_path(path, backup)
            prior_displaced = True
        _replace_path(temporary, path)
        new_committed = True
        _remaining(deadline)
        if _read_receipt(path) != dict(payload):
            raise LifecycleFailure(EXIT_INSTALL, "commit", "receipt_commit_invalid")
        _remaining(deadline)
        if prior_displaced:
            try:
                backup.unlink()
                prior_displaced = False
            except OSError:
                rollback()
                raise LifecycleFailure(EXIT_INSTALL, "cleanup", "receipt_cleanup_failed") from None
    except LifecycleFailure:
        rollback()
        raise
    except OSError as exc:
        rollback()
        cleanup_failed = False
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            cleanup_failed = True
        if cleanup_failed:
            raise LifecycleFailure(EXIT_INSTALL, "cleanup", "receipt_cleanup_failed") from None
        raise LifecycleFailure(EXIT_INSTALL, "commit", "receipt_commit_failed") from exc
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            if not prior_displaced and not new_committed:
                raise LifecycleFailure(EXIT_INSTALL, "cleanup", "receipt_cleanup_failed") from None


@contextmanager
def _mutation_lock(receipt_path: Path) -> Iterator[None]:
    _assert_safe_receipt_parent(receipt_path)
    lock_path = receipt_path.with_suffix(receipt_path.suffix + ".lock")
    if lock_path.exists():
        try:
            metadata = lock_path.lstat()
        except OSError as exc:
            raise LifecycleFailure(EXIT_ACQUIRE, "lock", "lifecycle_lock_unavailable") from exc
        if _is_reparse_or_symlink(lock_path, metadata) or not stat.S_ISREG(metadata.st_mode):
            raise LifecycleFailure(EXIT_ACQUIRE, "lock", "lifecycle_lock_unsafe")
    handle = None
    try:
        handle = lock_path.open("a+b")
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
    except OSError as exc:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        raise LifecycleFailure(EXIT_ACQUIRE, "lock", "lifecycle_lock_unavailable") from exc
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LifecycleFailure(EXIT_ACQUIRE, "lock", "lifecycle_lock_busy") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LifecycleFailure(EXIT_ACQUIRE, "lock", "lifecycle_lock_busy") from exc
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            handle.close()


def _step(identifier: str, status: str, description: str) -> Dict[str, str]:
    return {"id": identifier, "status": status, "description": description}


def _next_step(identifier: str, description: str, why: str, command: list[str]) -> Dict[str, Any]:
    return {"id": identifier, "description": description, "why": why, "command": command}


def _base_result(request: DoctorRequest, core_version: str) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "running",
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": core_version,
        "steps": [],
        "next_steps": [],
        "receipt_path": "configured",
        "verify": {"directly_usable": False, "failure_stage": None, "failure_reason": None},
        "command": request.operation,
        "requires_restart": False,
    }


def _failure_result(request: DoctorRequest, failure: LifecycleFailure) -> Dict[str, Any]:
    try:
        core_version = importlib_metadata.version("dcc-mcp-core")
    except importlib_metadata.PackageNotFoundError:
        core_version = "unavailable"
    result = _base_result(request, core_version)
    result.update({"status": "failed", "exit_code": failure.exit_code})
    result["verify"].update({"failure_stage": failure.stage, "failure_reason": failure.reason})
    result["steps"] = [_step(failure.stage, "failed", "The lifecycle operation failed closed.")]
    return result


def _verify_receipt_bindings(
    receipt: Mapping[str, Any], core: Mapping[str, Any], runtime: Mapping[str, Any]
) -> None:
    if receipt.get("adapter_version") != __version__:
        raise LifecycleFailure(EXIT_VERIFY, "receipt", "adapter_version_changed")
    recorded_core = receipt.get("core", {})
    if recorded_core.get("version") != core.get("version") or not _same_identity(
        recorded_core.get("module", {}), core.get("module", {})
    ):
        raise LifecycleFailure(EXIT_VERIFY, "identity", "core_identity_changed")
    if not _same_identity(receipt.get("python", {}), core.get("python", {})):
        raise LifecycleFailure(EXIT_VERIFY, "identity", "python_identity_changed")
    if not _same_identity(receipt.get("openscad", {}), runtime):
        raise LifecycleFailure(EXIT_VERIFY, "identity", "openscad_identity_changed")
    if receipt.get("openscad", {}).get("version") != runtime.get("version"):
        raise LifecycleFailure(EXIT_VERIFY, "identity", "openscad_version_changed")


def _run_lifecycle(request: DoctorRequest) -> Dict[str, Any]:
    if request.operation not in LIFECYCLE_COMMANDS:
        raise LifecycleFailure(EXIT_PREFLIGHT, "arguments", "operation_invalid")
    _validate_schema_loader()
    try:
        timeout = float(request.timeout_secs)
    except (TypeError, ValueError) as exc:
        raise LifecycleFailure(EXIT_PREFLIGHT, "arguments", "timeout_invalid") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > 1_800:
        raise LifecycleFailure(EXIT_PREFLIGHT, "arguments", "timeout_invalid")
    deadline = request.deadline if request.deadline is not None else time.monotonic() + timeout
    _remaining(deadline)
    receipt_path = _receipt_path(request.receipt_path)
    core_before = _capture_core_identity()
    _remaining(deadline)
    result = _base_result(request, str(core_before["version"]))
    receipt = _read_receipt(receipt_path)
    _remaining(deadline)

    if request.operation == "status":
        result["status"] = "ok" if receipt is not None else "partial"
        result["exit_code"] = EXIT_OK if receipt is not None else EXIT_PREFLIGHT
        result["steps"] = [
            _step(
                "receipt", "ok" if receipt is not None else "missing", "Inspect the owned receipt."
            )
        ]
        if receipt is None:
            result["verify"].update(
                {"failure_stage": "receipt", "failure_reason": "receipt_missing"}
            )
        return result

    if request.operation == "uninstall":
        if receipt is None:
            result.update({"status": "ok", "exit_code": EXIT_OK})
            result["steps"] = [
                _step("uninstall", "skipped", "The owned receipt is already absent.")
            ]
            return result
        if request.dry_run or not request.yes:
            result.update({"status": "planned", "exit_code": EXIT_OK})
            result["steps"] = [_step("uninstall", "planned", "Remove only the owned receipt.")]
            result["next_steps"] = [
                _next_step(
                    "confirm-uninstall",
                    "Confirm removal of the OpenSCAD adapter receipt.",
                    "OpenSCAD itself is never removed or cached by this adapter.",
                    ["dcc-mcp-openscad", "uninstall", "--yes", "--json"],
                )
            ]
            return result
        with _mutation_lock(receipt_path):
            _remaining(deadline)
            current = _read_receipt(receipt_path)
            _remaining(deadline)
            if current is None:
                result.update({"status": "ok", "exit_code": EXIT_OK})
                result["steps"] = [_step("uninstall", "skipped", "The owned receipt is absent.")]
                return result
            tombstone = receipt_path.parent / (
                ".%s.uninstall-%s" % (receipt_path.name, uuid.uuid4().hex)
            )
            try:
                _replace_path(receipt_path, tombstone)
                _remaining(deadline)
                tombstone.unlink()
            except LifecycleFailure:
                if tombstone.exists() and not receipt_path.exists():
                    try:
                        _replace_path(tombstone, receipt_path)
                    except OSError:
                        raise LifecycleFailure(
                            EXIT_INSTALL, "rollback", "uninstall_rollback_failed"
                        ) from None
                raise
            except OSError as exc:
                if tombstone.exists() and not receipt_path.exists():
                    try:
                        _replace_path(tombstone, receipt_path)
                    except OSError:
                        raise LifecycleFailure(
                            EXIT_INSTALL, "rollback", "uninstall_rollback_failed"
                        ) from None
                raise LifecycleFailure(EXIT_INSTALL, "uninstall", "uninstall_failed") from exc
        result.update({"status": "ok", "exit_code": EXIT_OK})
        result["steps"] = [_step("uninstall", "ok", "Removed the owned receipt.")]
        return result

    executable = _resolve_executable(request.executable)
    runtime = _capture_runtime(executable, deadline)
    core_after_probe = _capture_core_identity()
    _remaining(deadline)
    if core_before != core_after_probe:
        raise LifecycleFailure(EXIT_VERIFY, "identity", "core_identity_changed")
    result["runtime"] = {
        "product": runtime["product"],
        "version": runtime["version"],
        "sha256": runtime["sha256"],
        "capabilities": runtime["capabilities"],
    }

    if request.operation == "doctor":
        result.update({"status": "ok", "exit_code": EXIT_OK})
        result["verify"] = {"directly_usable": True, "failure_stage": None, "failure_reason": None}
        result["steps"] = [
            _step("preflight", "ok", "Core and the exact OpenSCAD runtime are supported."),
            _step("runtime", "ok", "OpenSCAD version and capabilities are verified."),
        ]
        return result

    if request.operation == "verify":
        if receipt is None:
            raise LifecycleFailure(EXIT_VERIFY, "receipt", "receipt_missing")
        _verify_receipt_bindings(receipt, core_before, runtime)
        _remaining(deadline)
        result.update({"status": "ok", "exit_code": EXIT_OK})
        result["verify"] = {"directly_usable": True, "failure_stage": None, "failure_reason": None}
        result["steps"] = [_step("verify", "ok", "Receipt and fresh runtime identities match.")]
        return result

    if request.operation == "upgrade" and receipt is None:
        raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_missing")
    if request.dry_run or not request.yes:
        result.update({"status": "planned", "exit_code": EXIT_OK})
        result["steps"] = [
            _step("preflight", "ok", "Core and OpenSCAD identities were captured."),
            _step("stage", "planned", "Stage one owned receipt in the destination directory."),
            _step("commit", "planned", "Atomically replace the owned receipt."),
            _step("verify", "planned", "Recapture every identity before declaring success."),
        ]
        result["next_steps"] = [
            _next_step(
                "confirm-%s" % request.operation,
                "Confirm the receipt transaction.",
                "The adapter never downloads, installs, or caches OpenSCAD itself.",
                ["dcc-mcp-openscad", request.operation, "--yes", "--json"],
            )
        ]
        return result

    with _mutation_lock(receipt_path):
        _remaining(deadline)
        current = _read_receipt(receipt_path)
        _remaining(deadline)
        if request.operation == "upgrade" and current is None:
            raise LifecycleFailure(EXIT_PREFLIGHT, "receipt", "receipt_missing")
        fresh_runtime = _capture_runtime(executable, deadline)
        fresh_core = _capture_core_identity()
        _remaining(deadline)
        if not _same_identity(runtime, fresh_runtime):
            raise LifecycleFailure(EXIT_VERIFY, "identity", "runtime_identity_changed")
        if core_before != fresh_core:
            raise LifecycleFailure(EXIT_VERIFY, "identity", "core_identity_changed")
        payload = _receipt_payload(fresh_core, fresh_runtime)
        if request.operation == "install" and current is not None and current != payload:
            raise LifecycleFailure(EXIT_INSTALL, "receipt", "upgrade_required")
        if current == payload:
            result.update({"status": "ok", "exit_code": EXIT_OK})
            result["verify"] = {
                "directly_usable": True,
                "failure_stage": None,
                "failure_reason": None,
            }
            result["steps"] = [_step("commit", "skipped", "The owned receipt already matches.")]
            return result
        _write_receipt_atomic(receipt_path, payload, deadline)
    result.update({"status": "ok", "exit_code": EXIT_OK})
    result["verify"] = {"directly_usable": True, "failure_stage": None, "failure_reason": None}
    result["steps"] = [
        _step("preflight", "ok", "Core and OpenSCAD identities are supported."),
        _step("stage", "ok", "Staged an ownership-bound receipt."),
        _step("commit", "ok", "Atomically committed the receipt."),
        _step("verify", "ok", "Fresh identities match the committed receipt."),
    ]
    return result


def run_doctor(request: DoctorRequest) -> Dict[str, Any]:
    """Run one lifecycle operation and always return a stable result envelope."""
    try:
        return _run_lifecycle(request)
    except LifecycleFailure as exc:
        return _failure_result(request, exc)
    except BaseException:
        return _failure_result(
            request, LifecycleFailure(EXIT_VERIFY, "internal", "lifecycle_internal_failure")
        )


run_lifecycle = run_doctor


__all__ = [
    "DoctorRequest",
    "LifecycleFailure",
    "LifecycleRequest",
    "LIFECYCLE_COMMANDS",
    "MINIMUM_CORE_VERSION",
    "SCHEMA_VERSION",
    "load_install_sop_schema",
    "run_doctor",
    "run_lifecycle",
]
