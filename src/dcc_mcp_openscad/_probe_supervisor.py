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


def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 7 or arguments[5] != "--":
        return 64
    status_path = Path(arguments[0])
    stdout_path = Path(arguments[1])
    stderr_path = Path(arguments[2])
    try:
        work_deadline = float(arguments[3])
        final_deadline = float(arguments[4])
    except ValueError:
        return 64
    if (
        not math.isfinite(work_deadline)
        or not math.isfinite(final_deadline)
        or work_deadline > final_deadline
    ):
        return 64
    command = arguments[6:]
    parent_pid = os.getppid()
    child = None
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
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
                    if os.name == "posix":
                        return _terminate_posix_group(
                            child, min(final_deadline, time.monotonic() + 0.2)
                        )
                    child.kill()
                    child.wait()
                    return 70
                if time.monotonic() >= work_deadline:
                    if os.name == "posix":
                        return _terminate_posix_group(child, final_deadline)
                    child.kill()
                    child.wait()
                    return 72
                time.sleep(0.02)
            returncode = child.wait()
            stdout_file.flush()
            stderr_file.flush()
            _write_status(status_path, {"state": "completed", "returncode": int(returncode)})

    # Retain the session/process-group leader until the owner has consumed the
    # terminal record and killed the whole owned tree. This prevents signaling
    # a recycled numeric process group after the probed root exits first.
    deadline = min(final_deadline, time.monotonic() + 60.0)
    while time.monotonic() < deadline:
        if os.getppid() != parent_pid:
            if os.name == "posix":
                return _terminate_posix_group(child, min(final_deadline, time.monotonic() + 0.2))
            return 70
        time.sleep(0.05)
    if os.name == "posix":
        return _terminate_posix_group(child, final_deadline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
