"""Post-write read-back contract for the mutating OpenSCAD tools.

The failure mode this module exists to eliminate is **"reported success, file
unchanged"**. ``openscad`` is a separate CLI process the adapter supervises; it
exits 0 on several paths that write nothing at all (an unrecognised output
suffix, an empty geometry, a silently swallowed export). An agent that receives
an affirmative answer keeps building on it, and the error only surfaces steps
later as an unrelated symptom.

The contract is one sentence:

    A mutating tool returns only after it has re-read the artifact from disk
    and proven that this call's change is actually there.

Anything else is a bug. Specifically, a mutating tool must never:

* report success because the CLI exited 0 without checking the artifact;
* return a summary computed from the inputs instead of from the file;
* report success when the write produced an empty or truncated geometry.

Read-back shape for a CLI host
------------------------------
There is no in-host interpreter to interrogate, so the read-back targets the
one durable thing a mutating call produces: the artifact on disk. Two levels
are checked.

1. **Identity**: the file at the final path exists, is non-empty, and its
   re-measured size and SHA-256 match what the export recorded. This catches
   the "exited 0, wrote nothing" and "wrote somewhere else" classes.
2. **Content**: the artifact is parsed back far enough to prove it carries the
   geometry that was asked for -- a binary STL whose declared facet count is
   zero or whose byte length contradicts that count, or a PNG whose IHDR
   dimensions differ from the requested ones, is a failed write even though
   the bytes are on disk.

Design notes for reuse
----------------------
Everything here is plain stdlib and knows nothing about OpenSCAD beyond the
artifact formats, so another CLI-backed adapter can copy this module and only
supply its own ``MUTATING_TOOLS`` / ``READ_ONLY_TOOLS`` and, if it writes
different formats, an extra content check.

Two properties matter more than the exact checks:

* **Expected and actual are always both reported.** A mismatch that only says
  "failed" makes the caller guess; the pair is what makes it actionable.
* **The host version is always attached.** A read-back that disagrees is the
  classic signature of host CLI drift, and without the version the report is
  unreproducible.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Any, List, Mapping, Optional, Sequence

SCHEMA_VERSION = 1

# Tools that write an artifact into the workspace. Every entry owes a read-back.
MUTATING_TOOLS = (
    "export_model",
    "render_preview",
)

# Tools that observe state and write nothing durable: `validate_model` compiles
# into a private temporary directory that is removed with the call, so nothing
# survives that a caller could depend on. Kept here so the classification test
# can prove no tool is left unclassified.
READ_ONLY_TOOLS = (
    "get_status",
    "get_capabilities",
    "inspect_source",
    "validate_model",
)

TOOL_CLASSIFICATION_ERROR = (
    "every tool must be listed in write_contract.MUTATING_TOOLS or "
    "write_contract.READ_ONLY_TOOLS; an unclassified tool has no answer to "
    "'does this owe a post-write read-back?'"
)

# Binary STL: an 80-byte header, a uint32 facet count, then 50 bytes per facet.
_STL_HEADER_BYTES = 84
_STL_FACET_BYTES = 50
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Geometry smaller than a single triangle is not a model.
_MIN_FACETS = 1


def jsonable(value):
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
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return repr(value)


def _describe(value):
    """Render one side of an expected/actual pair compactly."""
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(_describe(item) for item in value)
    if isinstance(value, dict):
        return "{%s}" % ", ".join("%s: %s" % (key, _describe(item)) for key, item in value.items())
    if isinstance(value, float):
        return repr(value)
    return str(value)


def numbers_match(expected, actual, rel_tolerance=1e-6, abs_tolerance=1e-9):
    """Compare two scalars."""
    try:
        return math.isclose(
            float(expected),
            float(actual),
            rel_tol=rel_tolerance,
            abs_tol=abs_tolerance,
        )
    except (TypeError, ValueError):
        return False


def sequences_match(expected, actual, rel_tolerance=1e-6, abs_tolerance=1e-9):
    """Compare two numeric sequences element by element.

    A length mismatch is a mismatch, not a truncated comparison.
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


def sha256_file(path) -> str:
    """Re-hash an artifact from disk. This is the read-back, not a cache read."""
    digest = hashlib.sha256()
    with open(str(path), "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_binary_stl(path) -> Optional[int]:
    """Return the declared facet count of a binary STL, or ``None``.

    ``None`` means "this is not a binary STL", which is not an error: an ASCII
    STL is a legitimate artifact and is checked separately.
    """
    with open(str(path), "rb") as stream:
        header = stream.read(_STL_HEADER_BYTES)
    if len(header) < _STL_HEADER_BYTES:
        return None
    if header[:5] == b"solid":
        return None
    return int(struct.unpack("<I", header[80:84])[0])


def read_ascii_stl_facets(path) -> Optional[int]:
    """Return the ``facet normal`` count of an ASCII STL, or ``None``."""
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as stream:
            text = stream.read()
    except OSError:
        return None
    if not text.lstrip().startswith("solid"):
        return None
    return text.count("facet normal")


def read_png_dimensions(path) -> Optional[List[int]]:
    """Return ``[width, height]`` from the PNG IHDR chunk, or ``None``."""
    with open(str(path), "rb") as stream:
        header = stream.read(24)
    if len(header) < 24 or header[:8] != _PNG_MAGIC or header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return [int(width), int(height)]


def format_message(payload):
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
        message += " (matrix status: %s)" % matrix["status"]
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
        tool,
        check,
        expected=None,
        actual=None,
        host_version=None,
        host_matrix=None,
        params=None,
        remediation=None,
        message=None,
    ):
        self.payload = {
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
    def tool(self):
        return self.payload.get("tool")

    @property
    def check(self):
        return self.payload.get("check")

    @property
    def expected(self):
        return self.payload.get("expected")

    @property
    def actual(self):
        return self.payload.get("actual")

    @property
    def host_version(self):
        return self.payload.get("host_version")

    @classmethod
    def from_payload(cls, payload):
        """Rebuild the error on the caller's side of a process boundary."""
        return cls(
            tool=payload.get("tool"),
            check=payload.get("check"),
            expected=payload.get("expected"),
            actual=payload.get("actual"),
            host_version=payload.get("host_version"),
            host_matrix=payload.get("host_matrix"),
            params=payload.get("params"),
            remediation=payload.get("remediation"),
            message=format_message(payload),
        )


def _mismatch(
    tool: str,
    check: str,
    expected: Any,
    actual: Any,
    *,
    host_version: Optional[str],
    host_matrix: Optional[Mapping[str, Any]],
    params: Optional[Mapping[str, Any]],
    remediation: Optional[str],
) -> "WriteVerificationError":
    return WriteVerificationError(
        tool=tool,
        check=check,
        expected=expected,
        actual=actual,
        host_version=host_version,
        host_matrix=host_matrix,
        params=params,
        remediation=remediation,
    )


def verify_artifact(
    tool: str,
    output_path,
    *,
    expected: Mapping[str, Any],
    host_version: Optional[str] = None,
    host_matrix: Optional[Mapping[str, Any]] = None,
    params: Optional[Mapping[str, Any]] = None,
) -> List[str]:
    """Read an exported artifact back and prove this call's change is there.

    ``expected`` carries what the export believed it wrote:
    ``{"sha256": ..., "bytes": ..., "suffix": ..., "width": ..., "height": ...}``.
    Returns the list of check ids that were proven. Raises
    :class:`WriteVerificationError` on the first disagreement.
    """
    import os

    path = str(output_path)
    suffix = str(expected.get("suffix") or "").lower()
    verified: List[str] = []

    # The exported identity is what makes the identity checks falsifiable.
    # Without it they degrade into "the file agrees with itself", which is the
    # one failure this contract must never let through, so it fails closed.
    if expected.get("sha256") is None or expected.get("bytes") is None:
        raise _mismatch(
            tool,
            "artifact.recorded_identity",
            "the sha256 and byte length recorded by the export",
            {"sha256": expected.get("sha256"), "bytes": expected.get("bytes")},
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation=(
                "the export must measure the artifact it writes and pass both values to the "
                "read-back; a read-back that re-measures the file cannot detect a write that "
                "was replaced or never landed"
            ),
        )

    # --- Identity -------------------------------------------------------
    if not os.path.isfile(path):
        raise _mismatch(
            tool,
            "artifact.exists",
            path,
            None,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation=(
                "OpenSCAD exited without producing an artifact; check the output suffix "
                "against `dcc-mcp-openscad get_capabilities`"
            ),
        )
    verified.append("artifact.exists")

    actual_bytes = int(os.path.getsize(path))
    expected_bytes = expected.get("bytes")
    if expected_bytes is not None and actual_bytes != int(expected_bytes):
        raise _mismatch(
            tool,
            "artifact.bytes",
            int(expected_bytes),
            actual_bytes,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="the artifact changed between the export and the read-back",
        )
    verified.append("artifact.bytes")

    if actual_bytes <= 0:
        raise _mismatch(
            tool,
            "artifact.non_empty",
            "a non-empty artifact",
            actual_bytes,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation=(
                "OpenSCAD wrote an empty file; the geometry is degenerate or the format was refused"
            ),
        )
    verified.append("artifact.non_empty")

    actual_sha256 = sha256_file(path)
    expected_sha256 = expected.get("sha256")
    if expected_sha256 is not None and actual_sha256 != str(expected_sha256):
        raise _mismatch(
            tool,
            "artifact.sha256",
            str(expected_sha256),
            actual_sha256,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="the artifact on disk is not the one the export produced",
        )
    verified.append("artifact.sha256")

    # --- Content --------------------------------------------------------
    if suffix == ".stl":
        verified.extend(_verify_stl(tool, path, actual_bytes, host_version, host_matrix, params))
    elif suffix == ".png":
        verified.extend(_verify_png(tool, path, expected, host_version, host_matrix, params))
    return verified


def _verify_stl(
    tool: str,
    path: str,
    actual_bytes: int,
    host_version: Optional[str],
    host_matrix: Optional[Mapping[str, Any]],
    params: Optional[Mapping[str, Any]],
) -> List[str]:
    """Prove the STL carries geometry, in whichever encoding it was written."""
    facets = read_binary_stl(path)
    if facets is None:
        facets = read_ascii_stl_facets(path)
        if facets is None:
            raise _mismatch(
                tool,
                "stl.encoding",
                "a binary or ASCII STL",
                "unrecognised",
                host_version=host_version,
                host_matrix=host_matrix,
                params=params,
                remediation="OpenSCAD wrote a file that is neither a binary nor an ASCII STL",
            )
        return _checked_facets(
            tool,
            facets,
            None,
            actual_bytes,
            host_version,
            host_matrix,
            params,
            "stl.ascii_facets",
            (),  # the ASCII encoding declares no length to reconcile
        )

    expected_size = _STL_HEADER_BYTES + facets * _STL_FACET_BYTES
    if expected_size != actual_bytes:
        raise _mismatch(
            tool,
            "stl.size_consistent",
            expected_size,
            actual_bytes,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation=(
                "the STL declares %d facet(s) but is %d byte(s) long; the export was truncated"
                % (facets, actual_bytes)
            ),
        )
    return _checked_facets(
        tool,
        facets,
        expected_size,
        actual_bytes,
        host_version,
        host_matrix,
        params,
        "stl.facets",
        ("stl.size_consistent",),
    )


def _checked_facets(
    tool: str,
    facets: int,
    expected_size: Optional[int],
    actual_bytes: int,
    host_version: Optional[str],
    host_matrix: Optional[Mapping[str, Any]],
    params: Optional[Mapping[str, Any]],
    check: str,
    extra: Sequence[str] = (),
) -> List[str]:
    if facets < _MIN_FACETS:
        raise _mismatch(
            tool,
            check,
            "at least %d facet" % _MIN_FACETS,
            facets,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation=(
                "OpenSCAD wrote an STL with no geometry; the model produced nothing to export"
            ),
        )
    del expected_size, actual_bytes
    # Every check that ran is reported, so a caller can tell which guards a
    # format actually got instead of inferring it from the ones that fired.
    return [check, "stl.non_empty_geometry"] + list(extra)


def _verify_png(
    tool: str,
    path: str,
    expected: Mapping[str, Any],
    host_version: Optional[str],
    host_matrix: Optional[Mapping[str, Any]],
    params: Optional[Mapping[str, Any]],
) -> List[str]:
    """Prove the PNG is a PNG and carries the requested frame size."""
    dimensions = read_png_dimensions(path)
    if dimensions is None:
        raise _mismatch(
            tool,
            "png.header",
            "a PNG IHDR header",
            "unrecognised",
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation=(
                "OpenSCAD wrote a file that is not a PNG; PNG output needs a GL-capable display"
            ),
        )
    expected_dimensions = _expected_dimensions(expected)
    if expected_dimensions is not None and list(dimensions) != list(expected_dimensions):
        raise _mismatch(
            tool,
            "png.dimensions",
            list(expected_dimensions),
            dimensions,
            host_version=host_version,
            host_matrix=host_matrix,
            params=params,
            remediation="OpenSCAD rendered a different frame size than the one requested",
        )
    return ["png.header", "png.dimensions"]


def _expected_dimensions(expected: Mapping[str, Any]) -> Optional[Sequence[int]]:
    width = expected.get("width")
    height = expected.get("height")
    if width is None or height is None:
        return None
    try:
        return [int(width), int(height)]
    except (TypeError, ValueError):
        return None


__all__ = [
    "MUTATING_TOOLS",
    "READ_ONLY_TOOLS",
    "SCHEMA_VERSION",
    "TOOL_CLASSIFICATION_ERROR",
    "WriteVerificationError",
    "format_message",
    "jsonable",
    "numbers_match",
    "read_ascii_stl_facets",
    "read_binary_stl",
    "read_png_dimensions",
    "sequences_match",
    "sha256_file",
    "verify_artifact",
]
