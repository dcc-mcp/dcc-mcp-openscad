"""Private process-tree supervisor for bounded OpenSCAD probes."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

_IS_POSIX = os.name == "posix"


def _write_status(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(str(temporary), str(path))


def _terminate_posix_group(child: Optional[subprocess.Popen], kill_deadline: float) -> int:
    leader_pid = os.getpid()
    if os.getpgrp() != leader_pid or os.getsid(0) != leader_pid:
        return 71
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        os.killpg(leader_pid, signal.SIGTERM)
    except ProcessLookupError:
        return 70
    while time.monotonic() < kill_deadline:
        if child is not None:
            child.poll()
        time.sleep(0.01)
    try:
        os.killpg(leader_pid, signal.SIGKILL)
    except ProcessLookupError:
        return 70
    return 70


def _fail_closed(child: Optional[subprocess.Popen], final_deadline: float) -> int:
    if _IS_POSIX:
        return _terminate_posix_group(child, final_deadline)
    if child is not None and child.poll() is None:
        child.kill()
        try:
            child.wait(timeout=max(0.0, final_deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    return 73


def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 8 or arguments[6] != "--":
        return 64
    status_path = Path(arguments[0])
    stdout_path = Path(arguments[1])
    stderr_path = Path(arguments[2])
    try:
        work_deadline = float(arguments[3])
        final_deadline = float(arguments[4])
        expected_parent_pid = int(arguments[5])
    except ValueError:
        return 64
    if (
        not math.isfinite(work_deadline)
        or not math.isfinite(final_deadline)
        or work_deadline > final_deadline
        or expected_parent_pid <= 0
    ):
        return 64
    parent_pid = expected_parent_pid if _IS_POSIX else os.getppid()
    if _IS_POSIX and os.getppid() != parent_pid:
        return 70
    if time.monotonic() >= work_deadline:
        return 72
    command = arguments[7:]
    child = None
    try:
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            if time.monotonic() >= work_deadline:
                return 72
            if _IS_POSIX and os.getppid() != parent_pid:
                return 70
            try:
                child = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    close_fds=True,
                )
            except OSError as exc:
                _write_status(
                    status_path,
                    {"state": "launch_failed", "error_type": exc.__class__.__name__},
                )
            else:
                while child.poll() is None:
                    if os.getppid() != parent_pid:
                        if _IS_POSIX:
                            return _terminate_posix_group(
                                child, min(final_deadline, time.monotonic() + 0.2)
                            )
                        child.kill()
                        child.wait()
                        return 70
                    if time.monotonic() >= work_deadline:
                        if _IS_POSIX:
                            return _terminate_posix_group(child, final_deadline)
                        child.kill()
                        child.wait()
                        return 72
                    time.sleep(0.02)
                returncode = child.wait()
                stdout_file.flush()
                stderr_file.flush()
                _write_status(
                    status_path,
                    {"state": "completed", "returncode": int(returncode)},
                )
    except BaseException:
        return _fail_closed(child, final_deadline)

    # Retain the session/process-group leader until the owner has consumed the
    # terminal record and killed the whole owned tree. This prevents signaling
    # a recycled numeric process group after the probed root exits first.
    deadline = min(final_deadline, time.monotonic() + 60.0)
    while time.monotonic() < deadline:
        if os.getppid() != parent_pid:
            if _IS_POSIX:
                return _terminate_posix_group(child, min(final_deadline, time.monotonic() + 0.2))
            return 70
        time.sleep(0.05)
    if _IS_POSIX:
        return _terminate_posix_group(child, final_deadline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
