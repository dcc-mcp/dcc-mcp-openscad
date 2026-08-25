"""Bounded OpenSCAD process execution with full-tree ownership."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from dcc_mcp_core.skills_helper import check_dcc_cancelled

_MAX_OUTPUT_BYTES = 65_536


class ProcessCleanupError(RuntimeError):
    """An owned process tree or its private probe directory survived cleanup."""


class _ProcessTreeOwner:
    def terminate(self) -> None:
        raise NotImplementedError

    def wait_empty(self, timeout: float) -> bool:
        del timeout
        return True

    def close(self) -> None:
        return None


class _PosixProcessTreeOwner(_ProcessTreeOwner):
    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process

    def terminate(self) -> None:
        if self._process.poll() is None:
            os.killpg(self._process.pid, 9)


class _WindowsProcessTreeOwner(_ProcessTreeOwner):
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class _BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._handle = handle
        self._accounting_type = _BasicAccountingInformation
        limits = _ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(handle)
            self._handle = None
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, process: subprocess.Popen) -> None:
        if not self._kernel32.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise OSError(self._ctypes.get_last_error(), "TerminateJobObject failed")

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._handle:
            accounting = self._accounting_type()
            if not self._kernel32.QueryInformationJobObject(
                self._handle,
                self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                self._ctypes.byref(accounting),
                self._ctypes.sizeof(accounting),
                None,
            ):
                return False
            if accounting.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _resume_windows_process(process: subprocess.Popen) -> None:
    import ctypes
    from ctypes import wintypes

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(_WindowsProcessTreeOwner._TH32CS_SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    resumed = False
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while present:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel32.OpenThread(
                    _WindowsProcessTreeOwner._THREAD_SUSPEND_RESUME,
                    False,
                    entry.th32ThreadID,
                )
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed = True
                    finally:
                        kernel32.CloseHandle(thread)
            present = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError("No suspended supervisor thread could be resumed")


def _start_owned_process(
    command: Sequence[str], *, env: Optional[Mapping[str, str]], cwd: Optional[Path]
) -> Tuple[subprocess.Popen, _ProcessTreeOwner]:
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": None if env is None else dict(env),
        "cwd": None if cwd is None else str(cwd),
    }
    if os.name == "posix":
        process = subprocess.Popen(list(command), start_new_session=True, **kwargs)
        return process, _PosixProcessTreeOwner(process)
    if os.name == "nt":
        owner = _WindowsProcessTreeOwner()
        process = None
        try:
            process = subprocess.Popen(
                list(command),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004,
                **kwargs,
            )
            owner.assign(process)
            _resume_windows_process(process)
            return process, owner
        except BaseException:
            try:
                owner.terminate()
            except OSError:
                if process is not None and process.poll() is None:
                    process.kill()
            if process is not None:
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
            owner.close()
            raise
    process = subprocess.Popen(list(command), **kwargs)
    return process, _ProcessTreeOwner()


def _cleanup_owned_process(process: subprocess.Popen, owner: _ProcessTreeOwner) -> bool:
    clean = True
    try:
        owner.terminate()
    except (NotImplementedError, OSError):
        clean = False
        if process.poll() is None:
            process.kill()
    try:
        process.wait(timeout=3.0)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            clean = False
    finally:
        try:
            if not owner.wait_empty(3.0):
                clean = False
        finally:
            owner.close()
    return clean


def _remove_probe_directory(root: Path, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            shutil.rmtree(str(root))
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


def run_bounded_command(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Run a fixed command under one owned process tree and absolute timeout."""
    root = Path(tempfile.mkdtemp(prefix="dcc-mcp-openscad-probe-"))
    status_path = root / "status.json"
    stdout_path = root / "stdout.bin"
    stderr_path = root / "stderr.bin"
    supervisor_path = Path(__file__).with_name("_probe_supervisor.py").resolve(strict=True)
    supervisor = [
        sys.executable,
        "-I",
        "-S",
        str(supervisor_path),
        str(status_path),
        str(stdout_path),
        str(stderr_path),
        "--",
        *[str(part) for part in command],
    ]
    process = None
    owner = None
    record = None
    reason = "probe_timeout"
    cleanup_ok = True
    pending_exception = None
    try:
        process, owner = _start_owned_process(supervisor, env=env, cwd=cwd or root)
        deadline = time.monotonic() + max(0.01, float(timeout))
        while time.monotonic() < deadline:
            check_dcc_cancelled()
            try:
                oversized = any(
                    path.is_file() and path.stat().st_size > _MAX_OUTPUT_BYTES
                    for path in (stdout_path, stderr_path)
                )
            except OSError:
                oversized = True
            if oversized:
                reason = "probe_output_limit"
                break
            if status_path.is_file():
                try:
                    value = json.loads(status_path.read_text(encoding="utf-8"))
                    record = value if isinstance(value, dict) else None
                except (OSError, ValueError):
                    reason = "probe_status_invalid"
                break
            if process.poll() is not None:
                reason = "probe_supervisor_failed"
                break
            time.sleep(0.01)
    except OSError:
        reason = "probe_launch_failed"
    except BaseException as exc:
        pending_exception = exc
    finally:
        if process is not None and owner is not None:
            cleanup_ok = _cleanup_owned_process(process, owner)

    try:
        stdout = stdout_path.read_bytes() if stdout_path.is_file() else b""
        stderr = stderr_path.read_bytes() if stderr_path.is_file() else b""
    except OSError:
        stdout = b""
        stderr = b""
        cleanup_ok = False
    truncated = len(stdout) > _MAX_OUTPUT_BYTES or len(stderr) > _MAX_OUTPUT_BYTES
    cleanup_ok = _remove_probe_directory(root) and cleanup_ok
    if pending_exception is not None:
        if not cleanup_ok:
            raise ProcessCleanupError("owned OpenSCAD process cleanup failed") from None
        raise pending_exception
    if not cleanup_ok:
        reason = "probe_cleanup_failed"
        record = None
    if record is None:
        return {
            "success": False,
            "reason": reason,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "truncated": truncated,
        }
    returncode = record.get("returncode")
    completed = record.get("state") == "completed" and isinstance(returncode, int)
    if not completed:
        terminal_reason = "probe_launch_failed"
    elif returncode != 0:
        terminal_reason = "probe_nonzero"
    elif truncated:
        terminal_reason = "probe_output_limit"
    else:
        terminal_reason = None
    return {
        "success": completed and returncode == 0 and not truncated,
        "reason": terminal_reason,
        "returncode": returncode if isinstance(returncode, int) else None,
        "stdout": stdout[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "stderr": stderr[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace"),
        "truncated": truncated,
    }


__all__ = ["ProcessCleanupError", "run_bounded_command"]
