"""Private process-tree supervisor for bounded OpenSCAD probes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional


def _write_status(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(str(temporary), str(path))


def main(argv: Optional[List[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 5 or arguments[3] != "--":
        return 64
    status_path = Path(arguments[0])
    stdout_path = Path(arguments[1])
    stderr_path = Path(arguments[2])
    command = arguments[4:]
    parent_pid = os.getppid()
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
            returncode = child.wait()
            stdout_file.flush()
            stderr_file.flush()
            _write_status(status_path, {"state": "completed", "returncode": int(returncode)})

    # Retain the session/process-group leader until the owner has consumed the
    # terminal record and killed the whole owned tree. This prevents signaling
    # a recycled numeric process group after the probed root exits first.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if os.getppid() != parent_pid:
            if os.name == "posix":
                os.killpg(os.getpgrp(), 9)
            return 70
        time.sleep(0.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
