"""Machine-readable OpenSCAD host compatibility matrix.

The matrix itself lives in ``compat_matrix.json`` next to this module so that
the install/doctor checks and any future in-host probe read one single source
of truth.

OpenSCAD versions are ``YYYY.MM`` release stamps, optionally ``YYYY.MM.DD`` for
the dated snapshot builds published at ``files.openscad.org/snapshots``. Both
shapes parse into the same ``(major, minor, patch)`` triple, so a range can
cover a release line and a snapshot line with one comparison.

A host version that is not covered by the matrix is reported as unsupported
with the covered ranges and a concrete remediation. Nothing here degrades
silently: an unrecognised or out-of-range version never becomes "good enough".
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

MATRIX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compat_matrix.json")

SUPPORTED = "supported"
TOO_OLD = "too_old"
TOO_NEW = "too_new"
UNLISTED = "unlisted"
UNKNOWN = "unknown"

STATUS_MESSAGES = {
    SUPPORTED: "supported",
    TOO_OLD: "below the supported range",
    TOO_NEW: "above the supported range",
    UNLISTED: "inside the covered span but not in any declared range",
    UNKNOWN: "not recognised as an OpenSCAD version",
}

# `2021.01`, `2021.01.3`, `2026.09.29` and any trailing build suffix.
_RELEASE = re.compile(r"^(\d{4})\.(\d{1,2})(?:\.(\d{1,4}))?")

Version = Tuple[int, int, int]


def parse_version(value: str) -> Optional[Version]:
    """Parse a ``YYYY.MM[.DD]`` OpenSCAD stamp into a comparable triple.

    Returns ``None`` for anything that is not an OpenSCAD stamp, which the
    caller turns into :data:`UNKNOWN` rather than a guess.
    """
    if not value:
        return None
    match = _RELEASE.match(str(value).strip())
    if match is None:
        return None
    year, month, patch = match.groups()
    if not (1 <= int(month) <= 12):
        return None
    return int(year), int(month), int(patch or 0)


def format_version(version: Version) -> str:
    return "%04d.%02d.%02d" % version


def load_matrix(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or MATRIX_PATH, "r", encoding="utf-8") as stream:
        return json.load(stream)


def supported_ranges(matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return list((matrix or load_matrix()).get("supported_ranges") or ())


def supported_range_labels(matrix: Optional[Dict[str, Any]] = None) -> List[str]:
    """Human-readable labels for the declared ranges, newest last."""
    labels = []
    for entry in supported_ranges(matrix):
        minimum = parse_version(str(entry.get("min_version", "")))
        maximum = parse_version(str(entry.get("max_version", "")))
        if minimum is None or maximum is None:
            continue
        if minimum[:2] == maximum[:2]:
            labels.append("%04d.%02d" % (minimum[0], minimum[1]))
        else:
            labels.append("%s-%s" % (_label(minimum), _label(maximum)))
    return labels


def _label(version: Version) -> str:
    return "%04d.%02d" % (version[0], version[1])


def breaking_changes_for(
    version: str, matrix: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Return the declared host CLI breaks that apply to ``version``."""
    matrix = matrix or load_matrix()
    parsed = parse_version(version)
    if parsed is None:
        return []
    applied = []
    for entry in matrix.get("breaking_changes") or ():
        since = parse_version(str(entry.get("applies_from", "")))
        if since is None or parsed < since:
            continue
        applied.append(entry)
    return applied


def classify_host(version: str, matrix: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Classify a discovered OpenSCAD version against the matrix.

    The result is machine readable and is embedded verbatim in the doctor and
    verify reports, so callers can gate on ``status`` instead of parsing prose.
    ``evidence_kind`` distinguishes a range backed by a real-hardware CI run
    from one backed only by static/contract review.
    """
    matrix = matrix or load_matrix()
    parsed = parse_version(version)
    verdict: Dict[str, Any] = {
        "version": version,
        "parsed_version": format_version(parsed) if parsed is not None else None,
        "status": UNKNOWN,
        "matrix_version": matrix.get("matrix_version"),
        "supported_ranges": supported_range_labels(matrix),
        "range": None,
        "evidence_kind": None,
        "breaking_changes": [
            {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "changed_in": entry.get("changed_in"),
                "kind": entry.get("kind"),
                "adapter_usage": entry.get("adapter_usage"),
                "enforcement": entry.get("enforcement"),
                "remediation": entry.get("remediation"),
            }
            for entry in breaking_changes_for(version, matrix)
        ],
    }
    if parsed is None:
        return verdict

    for entry in supported_ranges(matrix):
        minimum = parse_version(str(entry.get("min_version", "")))
        maximum = parse_version(str(entry.get("max_version", "")))
        if minimum is None or maximum is None:
            continue
        if minimum <= parsed <= maximum:
            verdict["status"] = SUPPORTED
            verdict["evidence_kind"] = entry.get("evidence_kind")
            verdict["range"] = {
                "id": entry.get("id"),
                "min_version": entry.get("min_version"),
                "max_version": entry.get("max_version"),
                "status": entry.get("status"),
                "evidence_kind": entry.get("evidence_kind"),
                "evidence": entry.get("evidence"),
                "ci_matrix_entry": entry.get("ci_matrix_entry"),
            }
            return verdict

    minimums = [
        parse_version(str(entry.get("min_version", ""))) for entry in supported_ranges(matrix)
    ]
    maximums = [
        parse_version(str(entry.get("max_version", ""))) for entry in supported_ranges(matrix)
    ]
    low = min([item for item in minimums if item is not None], default=None)
    high = max([item for item in maximums if item is not None], default=None)
    if low is not None and parsed < low:
        status = TOO_OLD
    elif high is not None and parsed > high:
        status = TOO_NEW
    else:
        # Inside the covered span but in a gap between declared ranges. That is
        # still outside the matrix and must not be treated as supported.
        status = UNLISTED
    verdict["status"] = status
    return verdict


def is_supported(version: str, matrix: Optional[Dict[str, Any]] = None) -> bool:
    return classify_host(version, matrix)["status"] == SUPPORTED


def unsupported_reason(verdict: Dict[str, Any]) -> str:
    """Build the human- and agent-readable rejection sentence for a verdict."""
    ranges = verdict.get("supported_ranges") or ()
    covered = ", ".join(ranges) if ranges else "no declared range"
    version = verdict.get("version") or "unknown"
    status = verdict.get("status")
    if status == UNKNOWN:
        return "OpenSCAD reported an unrecognised version %r; supported range: %s" % (
            version,
            covered,
        )
    if status == TOO_NEW:
        return (
            "OpenSCAD %s is newer than the verified compatibility matrix (supported: %s); "
            "the CLI surface may have moved, so the adapter refuses to run unverified"
            % (version, covered)
        )
    if status == UNLISTED:
        return (
            "OpenSCAD %s is not listed in the verified compatibility matrix (supported: %s); "
            "the adapter refuses to run unverified" % (version, covered)
        )
    return "OpenSCAD %s is unsupported; supported range: %s" % (version, covered)


__all__ = [
    "MATRIX_PATH",
    "SUPPORTED",
    "TOO_NEW",
    "TOO_OLD",
    "UNKNOWN",
    "UNLISTED",
    "classify_host",
    "format_version",
    "is_supported",
    "load_matrix",
    "parse_version",
    "supported_range_labels",
    "supported_ranges",
    "unsupported_reason",
]
