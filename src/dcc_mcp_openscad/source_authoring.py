"""Bounded, new-file-only authoring of self-contained SCAD source."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from .bridge import get_bridge

MAX_BYTES = 262144
WINDOWS_RESERVED_STEMS = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


def _file_identity(metadata):
    if (
        not stat.S_ISREG(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & 0x400
        or not metadata.st_ino
    ):
        return None
    return int(metadata.st_dev), int(metadata.st_ino)


def _matches_created_file(path, identity):
    if identity is None:
        return False
    try:
        return _file_identity(path.lstat()) == identity
    except OSError:
        return False


def _remove_created_file(path, identity):
    if _matches_created_file(path, identity):
        path.unlink()


def _validated_destination(raw):
    if ".." in raw.parts:
        raise ValueError("traversal and symlink paths are not accepted")
    for candidate in (raw, *raw.parents):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("symlink and reparse paths are not accepted")
    path = raw.resolve()
    roots = get_bridge().allowed_roots
    if not any(path != r and r in path.parents for r in roots):
        raise ValueError("output_path is outside configured workspace roots")
    if not path.parent.is_dir():
        raise ValueError("parent directory must already exist")
    return path


def write_model_source(output_path: str, source: str) -> dict:
    if not isinstance(source, str) or not source.strip() or "\x00" in source:
        raise ValueError("source must be nonempty text without NUL")
    data = source.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("source exceeds 262144 UTF-8 bytes")
    # This deliberately conservative contract accepts only self-contained models.
    if re.search(r"\b(include|use|import(?:_(?:stl|dxf|off))?|surface)\b", source, re.I):
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
    path = _validated_destination(raw)
    fd, temporary_name = tempfile.mkstemp(prefix=".dcc-mcp-scad-", dir=str(path.parent))
    temporary = Path(temporary_name)
    identity = None
    try:
        identity = _file_identity(os.fstat(fd))
        if identity is None:
            raise RuntimeError("SCAD temporary file identity could not be verified")
        stream = os.fdopen(fd, "w+b")
        fd = None  # The stream now owns the descriptor, including failure paths.
        with stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            readback = stream.read()
            if readback != data:
                raise RuntimeError("SCAD source readback did not match submitted bytes")
        if _validated_destination(raw) != path or not _matches_created_file(temporary, identity):
            raise RuntimeError("SCAD publication path identity changed")
        # Publish only complete, verified bytes. A hard link never replaces an
        # existing destination, including one created while the source was written.
        os.link(str(temporary), str(path))
        if not _matches_created_file(path, identity):
            raise RuntimeError("SCAD published file identity changed")
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            _remove_created_file(temporary, identity)
        except OSError:
            pass  # Preserve the write/publication failure rather than a cleanup error.
        raise
    else:
        try:
            _remove_created_file(temporary, identity)
        except OSError:
            pass  # A staging cleanup failure cannot undo a verified publication.
    return {
        "path": str(path),
        "bytes": len(readback),
        "sha256": hashlib.sha256(readback).hexdigest(),
        "verified": ["new_file_only", "workspace_root", "self_contained", "exact_utf8_readback"],
    }
