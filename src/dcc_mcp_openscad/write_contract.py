"""Post-write read-back contract for mutating tools.

The failure mode this module exists to eliminate is **"reported success, nothing
was written"**. In an agent loop that is the most expensive class of bug there
is: the caller receives an affirmative answer plus a plausible-looking SHA256,
keeps building on it, and the discrepancy only surfaces steps later as an
unrelated symptom.

The contract is one sentence:

    A mutating tool returns only after it has read the target back and proven
    that this call's change is actually there.

Anything else is a bug. Specifically, a mutating tool must never:

* return a success computed from the arguments instead of from the artifact;
* report a digest it never re-read from disk;
* report success when only part of the change applied.

Design notes for reusing this in another adapter
------------------------------------------------

Everything here is plain stdlib. Another adapter can copy this module and only
supply:

1. ``MUTATING_TOOLS`` / ``READ_ONLY_TOOLS`` -- the classification of its own
   method table. A tool in neither list fails the classification test, so
   adding a method forces the author to decide whether it owes a read-back.
2. A read-back helper per tool that calls :func:`verify_artifact` and lets
   :class:`WriteVerificationError` propagate.

Two properties matter more than the exact checks:

* **Expected and actual are always both reported.** A mismatch that only says
  "failed" makes the caller guess; the pair is what makes it actionable.
* **The host version is always attached.** A read-back that disagrees is the
  classic signature of host CLI drift, and without the version the report is
  unreproducible. ``host_matrix`` is attached alongside it so the caller can
  see whether that version is one the adapter has actually verified.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

# Tools that write a file the caller asked for. Every entry owes a read-back.
MUTATING_TOOLS = (
    "export_model",
    "render_preview",
)

# Tools that observe state and change nothing. `validate_model` compiles into a
# temporary directory that is discarded before it returns, so it owes no
# read-back of a caller-visible artifact. Kept here so the classification test
# can prove no public method is left unclassified.
READ_ONLY_TOOLS = (
    "status",
    "capabilities",
    "inspect_file",
    "validate_model",
)

TOOL_CLASSIFICATION_ERROR = (
    "every public OpenscadCli method must be listed in write_contract.MUTATING_TOOLS or "
    "write_contract.READ_ONLY_TOOLS; an unclassified method has no answer to "
    "'does this owe a post-write read-back?'"
)

# Byte layout of the two artifact families the adapter can produce.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_IHDR_WIDTH_OFFSET = 16
PNG_IHDR_HEIGHT_OFFSET = 20
PNG_MIN_BYTES = 24
STL_BINARY_HEADER_BYTES = 80
STL_BINARY_MIN_BYTES = 84
STL_BINARY_TRIANGLE_BYTES = 50

# Floating point read-back tolerance. Absorbs serialisation noise, not a real
# difference; deliberately far tighter than any modelling-relevant delta.
DEFAULT_REL_TOLERANCE = 1e-6
DEFAULT_ABS_TOLERANCE = 1e-9


def jsonable(value: Any) -> Any:
    """Coerce ``value`` into something ``json.dump`` accepts.

    Read-back evidence crosses a process boundary, so anything that cannot be
    represented in JSON is rendered as text rather than dropped: a dropped
    field is how a report ends up saying "expected something, got something".
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not valid JSON; keep them visible as text.
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return repr(value)


def _describe(value: Any) -> str:
    """Render one side of an expected/actual pair compactly."""
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(_describe(item) for item in value)
    if isinstance(value, dict):
        return "{%s}" % ", ".join("%s: %s" % (key, _describe(item)) for key, item in value.items())
    if isinstance(value, float):
        return repr(value)
    if value is None:
        return "None"
    return str(value)


def numbers_match(
    expected: Any,
    actual: Any,
    rel_tolerance: Optional[float] = None,
    abs_tolerance: Optional[float] = None,
) -> bool:
    """Compare two scalars with the contract's default tolerance."""
    try:
        return math.isclose(
            float(expected),
            float(actual),
            rel_tol=DEFAULT_REL_TOLERANCE if rel_tolerance is None else rel_tolerance,
            abs_tol=DEFAULT_ABS_TOLERANCE if abs_tolerance is None else abs_tolerance,
        )
    except (TypeError, ValueError):
        return False


def sequences_match(
    expected: Sequence[Any],
    actual: Sequence[Any],
    rel_tolerance: Optional[float] = None,
    abs_tolerance: Optional[float] = None,
) -> bool:
    """Compare two numeric sequences element by element.

    A length mismatch is a mismatch, not a truncated comparison: reporting
    three coordinates against two would hide the difference.
    """
    try:
        expected = [float(item) for item in expected]
        actual = [float(item) for item in actual]
    except (TypeError, ValueError):
        return False
    if len(expected) != len(actual):
        return False
    return all(
        numbers_match(item, other, rel_tolerance, abs_tolerance)
        for item, other in zip(expected, actual)
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_png_size(path: Path) -> Optional[Tuple[int, int]]:
    """Return ``(width, height)`` from a PNG IHDR, or ``None`` if unusable.

    Returns ``None`` rather than raising: a caller that renders a PNG wants the
    dimension mismatch reported as an expected/actual pair, not as a parse
    crash that hides which side was wrong.
    """
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(PNG_MIN_BYTES)
    except OSError:
        return None
    if len(header) < PNG_MIN_BYTES or header[:8] != PNG_SIGNATURE:
        return None
    if header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[PNG_IHDR_WIDTH_OFFSET : PNG_IHDR_HEIGHT_OFFSET + 4])
    return int(width), int(height)


def read_stl_shape(path: Path) -> Optional[Dict[str, Any]]:
    """Return the declared triangle count and implied size of an STL file.

    Covers both encodings the adapter can request: binary STL (80-byte header
    plus a ``uint32`` triangle count) and ASCII STL (``solid`` prologue, where
    the count is not declared and only the encoding is reportable).
    """
    try:
        with Path(path).open("rb") as stream:
            header = stream.read(STL_BINARY_MIN_BYTES)
        size = Path(path).stat().st_size
    except OSError:
        return None
    if not header:
        return None
    # The `solid` prologue is checked before the length guard: an ASCII STL
    # declares no triangle count and is legitimately shorter than a binary
    # header, so requiring 84 bytes first would misreport it as unreadable.
    if header[:5] == b"solid":
        return {"encoding": "ascii", "triangles": None, "size": size}
    if len(header) < STL_BINARY_MIN_BYTES:
        return None
    count = struct.unpack("<I", header[STL_BINARY_HEADER_BYTES:STL_BINARY_MIN_BYTES])[0]
    return {"encoding": "binary", "triangles": int(count), "size": size}


def verify_artifact(
    tool: str,
    path: Path,
    expected: Mapping[str, Any],
    host_version: Optional[str] = None,
    host_matrix: Optional[Mapping[str, Any]] = None,
    params: Optional[Mapping[str, Any]] = None,
    extra_checks: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Read ``path`` back and prove the committed artifact is the one we made.

    ``expected`` carries what the tool believed it wrote (size, sha256 and, when
    known, the mtime it hashed). Every field is re-read from disk here; nothing
    is carried over from the write. The returned evidence dict is what the tool
    puts in its result, so a caller can see the read-back actually happened.
    """
    target = Path(path)
    evidence: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "path": str(target),
    }

    # 1. The file is there and is a regular file -- not a directory, a broken
    #    symlink, or a path that only existed during the write.
    try:
        stat_result = target.lstat()
    except OSError as exc:
        raise WriteVerificationError(
            tool,
            "artifact_exists",
            expected=True,
            actual=False,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="The OpenSCAD CLI exited without leaving the output file; "
            "re-run validate_model against the same source before retrying.",
        ) from exc
    if not target.is_file():
        raise WriteVerificationError(
            tool,
            "artifact_is_regular_file",
            expected="regular file",
            actual="not a regular file",
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
        )

    actual_mtime_ns = int(getattr(stat_result, "st_mtime_ns", int(stat_result.st_mtime * 1e9)))
    actual_size = int(stat_result.st_size)
    evidence["size"] = actual_size
    evidence["mtime_ns"] = actual_mtime_ns

    # 2. Non-empty. A zero-byte artifact is the signature of a host that created
    #    the file and then failed to write into it.
    if actual_size <= 0:
        raise WriteVerificationError(
            tool,
            "artifact_non_empty",
            expected="size > 0",
            actual=actual_size,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="OpenSCAD created an empty file; the model produced no geometry.",
        )

    # 3. Size matches what the write believed it committed.
    if "size" in expected and not numbers_match(expected["size"], actual_size):
        raise WriteVerificationError(
            tool,
            "artifact_size",
            expected=expected["size"],
            actual=actual_size,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="The file changed between the export and the read-back.",
        )

    # 4. Re-hash from disk. This is the check that makes the digest in the
    #    result trustworthy: it was read back, not remembered.
    actual_sha256 = sha256_file(target)
    evidence["sha256"] = actual_sha256
    if "sha256" in expected and str(expected["sha256"]) != actual_sha256:
        raise WriteVerificationError(
            tool,
            "artifact_sha256",
            expected=expected["sha256"],
            actual=actual_sha256,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="The artifact on disk is not the one this call wrote.",
        )

    # 5. The file was not swapped after hashing.
    if "mtime_ns" in expected and int(expected["mtime_ns"]) != actual_mtime_ns:
        raise WriteVerificationError(
            tool,
            "artifact_mtime_ns",
            expected=int(expected["mtime_ns"]),
            actual=actual_mtime_ns,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="The artifact was modified after this call hashed it.",
        )

    # 6. Caller-supplied format assertions (PNG dimensions, STL encoding, ...).
    #
    # Each entry may be a plain mapping or a zero-argument callable. A callable
    # is resolved here rather than at call time, because these checks read the
    # artifact and therefore only make sense after the write has committed.
    for entry in extra_checks or ():
        check = entry() if callable(entry) else entry
        expected = check.get("expected")
        actual = check.get("actual")
        # Sequences compare element-wise so a length mismatch is a mismatch;
        # strings and scalars compare directly.
        if isinstance(expected, (list, tuple)):
            matched = sequences_match(expected, actual) if actual is not None else False
        elif isinstance(expected, str) or isinstance(actual, str):
            matched = expected == actual
        else:
            matched = numbers_match(expected, actual)
        if not matched:
            raise WriteVerificationError(
                tool,
                str(check.get("check")),
                expected=expected,
                actual=actual,
                host_version=host_version,
                host_matrix=host_matrix,
                params=params,
                remediation=check.get("remediation"),
            )
        evidence[str(check.get("check"))] = jsonable(actual)

    return evidence


def format_message(payload: Mapping[str, Any]) -> str:
    """Render the human- and agent-readable sentence for a mismatch.

    Deliberately states the tool, the check, both values, and the host version
    in that order: the reader should never have to re-run the call to find out
    what differed.
    """
    tool = payload.get("tool") or "unknown tool"
    check = payload.get("check") or "unknown check"
    message = (
        "%s did not take effect: the post-write read-back disagreed on %s "
        "(expected %s, read back %s)"
        % (
            tool,
            check,
            _describe(payload.get("expected")),
            _describe(payload.get("actual")),
        )
    )
    version = payload.get("host_version")
    if version:
        message += "; host OpenSCAD %s" % version
    matrix = payload.get("host_matrix") or {}
    if matrix.get("status"):
        message += " (matrix status: %s" % matrix["status"]
        if matrix.get("evidence_kind"):
            message += ", evidence: %s)" % matrix["evidence_kind"]
        else:
            message += ")"
    remediation = payload.get("remediation")
    if remediation:
        message += ". %s" % remediation
    return message


class WriteVerificationError(RuntimeError):
    """A mutating tool reported success but the read-back disagreed.

    The structured :attr:`payload` travels with the exception so the boundary
    that serialises the error can forward it verbatim and the caller can branch
    on ``check``/``expected``/``actual`` instead of parsing prose.
    """

    def __init__(
        self,
        tool: str,
        check: str,
        expected: Any = None,
        actual: Any = None,
        host_version: Optional[str] = None,
        host_matrix: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        remediation: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        self.payload: Dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "check": check,
            "expected": jsonable(expected),
            "actual": jsonable(actual),
            "host_version": host_version,
            "host_matrix": jsonable(host_matrix),
            "params": jsonable(params),
            "remediation": remediation,
        }
        super().__init__(message or format_message(self.payload))

    @property
    def tool(self) -> Any:
        return self.payload.get("tool")

    @property
    def check(self) -> Any:
        return self.payload.get("check")

    @property
    def expected(self) -> Any:
        return self.payload.get("expected")

    @property
    def actual(self) -> Any:
        return self.payload.get("actual")

    @property
    def host_version(self) -> Any:
        return self.payload.get("host_version")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WriteVerificationError":
        """Rebuild the error on the caller's side of a process boundary."""
        return cls(
            tool=str(payload.get("tool") or ""),
            check=str(payload.get("check") or ""),
            expected=payload.get("expected"),
            actual=payload.get("actual"),
            host_version=payload.get("host_version"),
            host_matrix=payload.get("host_matrix"),
            params=payload.get("params"),
            remediation=payload.get("remediation"),
            message=format_message(payload),
        )


__all__ = [
    "MUTATING_TOOLS",
    "READ_ONLY_TOOLS",
    "TOOL_CLASSIFICATION_ERROR",
    "WriteVerificationError",
    "format_message",
    "jsonable",
    "numbers_match",
    "read_png_size",
    "read_stl_shape",
    "sequences_match",
    "sha256_file",
    "verify_artifact",
]
