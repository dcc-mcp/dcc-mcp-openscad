from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class OpenScadError(RuntimeError):
    pass


class OpenscadCli:
    def __init__(self, executable=None):
        self.executable = executable

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get("DCC_MCP_OPENSCAD_EXECUTABLE", "")
            or shutil.which("openscad")
        )

    def status(self) -> dict[str, Any]:
        if not self.executable:
            return {"ready": False, "executable": None, "mode": "standalone CLI"}
        r = subprocess.run(
            [self.executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return {
            "ready": r.returncode == 0,
            "executable": self.executable,
            "mode": "standalone CLI",
            "version": (r.stdout or r.stderr).strip(),
        }

    def inspect_file(self, path: str) -> dict[str, Any]:
        p = Path(path).expanduser().resolve()
        if p.suffix.lower() != ".scad":
            raise OpenScadError("Only .scad files are accepted")
        if not p.is_file():
            raise OpenScadError(f"SCAD file does not exist: {p}")
        text = p.read_text(encoding="utf-8")
        return {
            "path": str(p),
            "bytes": p.stat().st_size,
            "lines": len(text.splitlines()),
            "has_module": "module " in text,
            "has_include": "include " in text,
        }


def get_bridge():
    return OpenscadCli.from_env()
