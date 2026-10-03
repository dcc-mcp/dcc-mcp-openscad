"""Bounded, new-file-only authoring of self-contained SCAD source."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .bridge import get_bridge

MAX_BYTES = 262144
WINDOWS_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


def write_model_source(output_path: str, source: str) -> dict:
    if not isinstance(source, str) or not source.strip() or "\x00" in source:
        raise ValueError("source must be nonempty text without NUL")
    data = source.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("source exceeds 262144 UTF-8 bytes")
    # This deliberately conservative contract accepts only self-contained models.
    if re.search(r"\b(include|use|import|surface)\b", source, re.I):
        raise ValueError("external source and data references are not accepted")
    raw = Path(output_path).expanduser()
    if not raw.is_absolute() or raw.suffix.lower() != ".scad":
        raise ValueError("output_path must be an absolute .scad path")
    if os.name == "nt" and any(
        ":" in part
        or part.partition(".")[0].rstrip(" ").upper() in WINDOWS_RESERVED_STEMS
        or part.endswith((" ", "."))
        for part in raw.parts[1:]
    ):
        raise ValueError("output_path must use ordinary filename components")
    if ".." in raw.parts or any(p.is_symlink() for p in (raw, *raw.parents)):
        raise ValueError("traversal and symlink paths are not accepted")
    path = raw.resolve()
    roots = get_bridge().allowed_roots
    if not any(path != r and r in path.parents for r in roots):
        raise ValueError("output_path is outside configured workspace roots")
    if not path.parent.is_dir():
        raise ValueError("parent directory must already exist")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    readback = path.read_bytes()
    if readback != data:
        raise RuntimeError("SCAD source readback did not match submitted bytes")
    return {
        "path": str(path),
        "bytes": len(readback),
        "sha256": hashlib.sha256(readback).hexdigest(),
        "verified": ["new_file_only", "workspace_root", "self_contained", "exact_utf8_readback"],
    }
