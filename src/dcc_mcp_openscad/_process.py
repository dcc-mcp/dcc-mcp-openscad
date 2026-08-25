"""Bounded OpenSCAD process execution with full-tree ownership."""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
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


def _linux_proc_live_members(pgid: int, sid: int, deadline: float) -> Optional[Sequence[int]]:
    proc_root = Path("/proc")
    if not (proc_root / "self" / "stat").is_file():
        return None
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return None
    live_members = []
    for entry in entries:
        if time.monotonic() >= deadline:
            return None
        if not entry.name.isdigit():
            continue
        try:
            stat_text = (entry / "stat").read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except OSError:
            return None
        try:
            fields = stat_text.rsplit(")", 1)[1].split()
            state = fields[0]
            observed_pgid = int(fields[2])
            observed_sid = int(fields[3])
        except (IndexError, ValueError):
            return None
        if (observed_pgid == pgid or observed_sid == sid) and not state.startswith("Z"):
            live_members.append(int(entry.name))
    return live_members


def _ps_live_members(pgid: int, sid: int, deadline: float) -> Optional[Sequence[int]]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    ps_path = next(
        (path for path in (Path("/bin/ps"), Path("/usr/bin/ps")) if path.is_file()), None
    )
    if ps_path is None:
        return None
    observed = None
    for session_column in ("sid", "sess"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            candidate = subprocess.run(
                [str(ps_path), "-axo", "pid=,pgid=,%s=,state=" % session_column],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=remaining,
                check=False,
                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if candidate.returncode == 0:
            observed = candidate
            break
    if observed is None:
        return None
    live_members = []
    for line in observed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 4:
            return None
        try:
            pid, observed_pgid, observed_sid = (int(value) for value in fields[:3])
        except ValueError:
            return None
        state = fields[3]
        if (observed_pgid == pgid or observed_sid == sid) and not state.startswith("Z"):
            live_members.append(pid)
    return live_members


def _posix_live_members(pgid: int, sid: int, deadline: float) -> Optional[Sequence[int]]:
    if (Path("/proc") / "self" / "stat").is_file():
        return _linux_proc_live_members(pgid, sid, deadline)
    return _ps_live_members(pgid, sid, deadline)


class _PosixProcessTreeOwner(_ProcessTreeOwner):
    def __init__(self, process: subprocess.Popen) -> None:
        self._process = process
        self._leader_pid = process.pid
        self._pgid = os.getpgid(process.pid)
        self._sid = os.getsid(process.pid)
        if self._pgid != self._leader_pid or self._sid != self._leader_pid:
            raise OSError("owned POSIX process is not its session leader")

    def terminate(self) -> None:
        if self._process.poll() is not None:
            raise ProcessLookupError("owned POSIX session leader is no longer live")
        if os.getpgid(self._leader_pid) != self._pgid or os.getsid(self._leader_pid) != self._sid:
            raise ProcessLookupError("owned POSIX session leader identity changed")
        os.killpg(self._pgid, signal.SIGKILL)

    def wait_empty(self, timeout: float) -> bool:
        if timeout <= 0:
            return False
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            live_members = _posix_live_members(self._pgid, self._sid, deadline)
            if live_members is None:
                return False
            if not live_members:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


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
        if timeout <= 0:
            return False
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
    command: Sequence[str],
    *,
    env: Optional[Mapping[str, str]],
    cwd: Optional[Path],
    deadline: float,
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
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            owner.close()
            raise
    process = subprocess.Popen(list(command), **kwargs)
    return process, _ProcessTreeOwner()


def _cleanup_owned_process(
    process: subprocess.Popen, owner: _ProcessTreeOwner, deadline: float
) -> bool:
    clean = True
    try:
        owner.terminate()
    except Exception:
        clean = False
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            clean = False
    try:
        if not owner.wait_empty(max(0.0, deadline - time.monotonic())):
            clean = False
    except Exception:
        clean = False
    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except (OSError, subprocess.TimeoutExpired):
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            clean = False
        try:
            still_running = process.poll() is None
        except OSError:
            still_running = True
        if still_running:
            clean = False
    try:
        owner.close()
    except Exception:
        clean = False
    return clean


def _remove_probe_directory(root: Path, deadline: float) -> bool:
    while True:
        try:
            shutil.rmtree(str(root))
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))


def _read_bounded_output(path: Path, deadline: float) -> Tuple[bytes, bool, bool]:
    """Read at most the public limit plus one byte under the caller deadline."""
    if time.monotonic() >= deadline:
        return b"", True, False
    try:
        if not path.is_file():
            return b"", True, time.monotonic() < deadline
        if time.monotonic() >= deadline:
            return b"", True, False
        with path.open("rb") as stream:
            if time.monotonic() >= deadline:
                return b"", True, False
            value = stream.read(_MAX_OUTPUT_BYTES + 1)
    except OSError:
        return b"", False, time.monotonic() < deadline
    if time.monotonic() >= deadline:
        return b"", True, False
    return value, True, True


def run_bounded_command(
    command: Sequence[str],
    *,
    timeout: float,
    cwd: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Run a fixed command under one owned process tree and absolute timeout."""
    budget = float(timeout)
    if not math.isfinite(budget):
        raise ValueError("timeout must be finite")
    budget = max(0.01, budget)
    deadline = time.monotonic() + budget
    cleanup_reserve = min(budget / 2.0, max(0.01, min(0.25, budget * 0.2)))
    work_deadline = deadline - cleanup_reserve
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
        repr(work_deadline),
        repr(deadline),
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
        if time.monotonic() < work_deadline:
            process, owner = _start_owned_process(
                supervisor,
                env=env,
                cwd=cwd or root,
                deadline=deadline,
            )
            while time.monotonic() < work_deadline:
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
                time.sleep(min(0.01, max(0.0, work_deadline - time.monotonic())))
    except OSError:
        reason = "probe_launch_failed"
    except BaseException as exc:
        pending_exception = exc
    finally:
        if process is not None and owner is not None:
            cleanup_ok = _cleanup_owned_process(process, owner, deadline)

    stdout, stdout_ok, stdout_in_budget = _read_bounded_output(stdout_path, deadline)
    if stdout_in_budget:
        stderr, stderr_ok, stderr_in_budget = _read_bounded_output(stderr_path, deadline)
    else:
        stderr, stderr_ok, stderr_in_budget = b"", True, False
    if not stdout_ok or not stderr_ok:
        cleanup_ok = False
    truncated = len(stdout) > _MAX_OUTPUT_BYTES or len(stderr) > _MAX_OUTPUT_BYTES
    cleanup_ok = _remove_probe_directory(root, deadline) and cleanup_ok
    deadline_expired = not stdout_in_budget or not stderr_in_budget or time.monotonic() >= deadline
    if pending_exception is not None:
        if not cleanup_ok:
            raise ProcessCleanupError("owned OpenSCAD process cleanup failed") from None
        raise pending_exception
    if not cleanup_ok:
        reason = "probe_cleanup_failed"
        record = None
    elif deadline_expired:
        reason = "probe_timeout"
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
