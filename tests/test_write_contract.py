"""Tests for the post-write read-back contract."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from dcc_mcp_openscad import write_contract as wc
from dcc_mcp_openscad.bridge import OpenscadCli


def _png(width: int, height: int) -> bytes:
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _binary_stl(triangles: int = 2) -> bytes:
    return b"h".ljust(80, b"\x00") + struct.pack("<I", triangles) + b"\x00" * (50 * triangles)


def _expected(path: Path) -> dict:
    stat = path.stat()
    return {
        "size": stat.st_size,
        "sha256": wc.sha256_file(path),
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)),
    }


# --------------------------------------------------------------------------
# Tool classification
# --------------------------------------------------------------------------


def test_every_public_cli_method_is_classified() -> None:
    """An unclassified method has no answer to 'does this owe a read-back?'."""
    # `from_env` is a factory: it builds an instance and dispatches no host
    # operation, so it is excluded rather than classified as a tool.
    factories = {"from_env"}
    public = {
        name
        for name in dir(OpenscadCli)
        if not name.startswith("_")
        and callable(getattr(OpenscadCli, name))
        and name not in factories
    }
    classified = set(wc.MUTATING_TOOLS) | set(wc.READ_ONLY_TOOLS)
    unclassified = public - classified
    assert not unclassified, wc.TOOL_CLASSIFICATION_ERROR + " unclassified: %s" % sorted(
        unclassified
    )
    # Every classified name must still be real, or the lists rot silently.
    assert classified <= public | factories


def test_classified_tools_exist_on_the_cli() -> None:
    for name in wc.MUTATING_TOOLS + wc.READ_ONLY_TOOLS:
        assert callable(getattr(OpenscadCli, name, None)), name


def test_the_two_destructive_skill_tools_are_the_mutating_ones() -> None:
    """Cross-check the contract against the declared MCP tool annotations."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    tools = yaml.safe_load(
        (
            root / "src" / "dcc_mcp_openscad" / "skills" / "openscad-pipeline" / "tools.yaml"
        ).read_text(encoding="utf-8")
    )["tools"]
    destructive = {t["name"] for t in tools if t["annotations"]["destructive_hint"]}
    read_only = {t["name"] for t in tools if t["annotations"]["read_only_hint"]}
    # Skill names map onto bridge methods; every destructive skill must owe a
    # read-back and no read-only skill may be treated as mutating.
    assert destructive == {"export_model", "render_preview"}
    assert destructive == set(wc.MUTATING_TOOLS)
    assert not (read_only & set(wc.MUTATING_TOOLS))


# --------------------------------------------------------------------------
# verify_artifact: happy paths
# --------------------------------------------------------------------------


def test_verify_artifact_returns_re_read_evidence(tmp_path: Path) -> None:
    target = tmp_path / "model.stl"
    target.write_bytes(_binary_stl())

    evidence = wc.verify_artifact("export_model", target, expected=_expected(target))

    assert evidence["sha256"] == wc.sha256_file(target)
    assert evidence["size"] == target.stat().st_size
    assert evidence["path"] == str(target)


def test_verify_artifact_checks_png_dimensions(tmp_path: Path) -> None:
    target = tmp_path / "preview.png"
    target.write_bytes(_png(640, 480))

    evidence = wc.verify_artifact(
        "render_preview",
        target,
        expected=_expected(target),
        extra_checks=[
            lambda: {
                "check": "png_dimensions",
                "expected": [640, 480],
                "actual": list(wc.read_png_size(target) or ()),
                "remediation": "host ignored --imgsize",
            }
        ],
    )

    assert evidence["png_dimensions"] == [640, 480]


# --------------------------------------------------------------------------
# verify_artifact: failure modes
# --------------------------------------------------------------------------


def test_missing_artifact_fails_with_expected_actual(tmp_path: Path) -> None:
    with pytest.raises(wc.WriteVerificationError) as raised:
        wc.verify_artifact("export_model", tmp_path / "absent.stl", expected={"size": 10})

    payload = raised.value.payload
    assert payload["tool"] == "export_model"
    assert payload["check"] == "artifact_exists"
    assert payload["expected"] is True
    assert payload["actual"] is False


def test_empty_artifact_fails(tmp_path: Path) -> None:
    target = tmp_path / "empty.stl"
    target.write_bytes(b"")

    with pytest.raises(wc.WriteVerificationError) as raised:
        wc.verify_artifact("export_model", target, expected={})

    assert raised.value.payload["check"] == "artifact_non_empty"
    assert raised.value.payload["actual"] == 0


def test_size_mismatch_is_reported_as_a_pair(tmp_path: Path) -> None:
    target = tmp_path / "model.stl"
    target.write_bytes(_binary_stl())

    with pytest.raises(wc.WriteVerificationError) as raised:
        wc.verify_artifact("export_model", target, expected={"size": 999_999})

    payload = raised.value.payload
    assert payload["check"] == "artifact_size"
    assert payload["expected"] == 999_999
    assert payload["actual"] == target.stat().st_size


def test_sha256_mismatch_is_detected_by_re_reading(tmp_path: Path) -> None:
    target = tmp_path / "model.stl"
    target.write_bytes(_binary_stl())
    expected = _expected(target)
    # Swapped for different content of the *same* length, so the size guard
    # passes and the digest is what catches it.
    swapped = bytearray(_binary_stl())
    swapped[-1] ^= 0xFF
    target.write_bytes(bytes(swapped))
    assert target.stat().st_size == expected["size"]

    with pytest.raises(wc.WriteVerificationError) as raised:
        wc.verify_artifact("export_model", target, expected=expected)

    payload = raised.value.payload
    assert payload["check"] == "artifact_sha256"
    assert payload["expected"] == expected["sha256"]
    assert payload["actual"] == wc.sha256_file(target)
    assert payload["expected"] != payload["actual"]


def test_mtime_mismatch_detects_a_post_hash_swap(tmp_path: Path) -> None:
    target = tmp_path / "model.stl"
    target.write_bytes(_binary_stl())
    expected = _expected(target)
    expected["mtime_ns"] = 1

    with pytest.raises(wc.WriteVerificationError) as raised:
        wc.verify_artifact("export_model", target, expected=expected)

    assert raised.value.payload["check"] == "artifact_mtime_ns"


def test_format_check_failure_carries_the_host_version(tmp_path: Path) -> None:
    target = tmp_path / "preview.png"
    target.write_bytes(_png(999, 999))

    with pytest.raises(wc.WriteVerificationError) as raised:
        wc.verify_artifact(
            "render_preview",
            target,
            expected=_expected(target),
            host_version="2026.09.29",
            host_matrix={"status": "supported", "evidence_kind": "real_ci"},
            extra_checks=[
                lambda: {
                    "check": "png_dimensions",
                    "expected": [640, 480],
                    "actual": list(wc.read_png_size(target) or ()),
                    "remediation": "host ignored --imgsize",
                }
            ],
        )

    payload = raised.value.payload
    assert payload["check"] == "png_dimensions"
    assert payload["expected"] == [640, 480]
    assert payload["actual"] == [999, 999]
    assert payload["host_version"] == "2026.09.29"
    assert payload["host_matrix"]["evidence_kind"] == "real_ci"
    # The message states the triple in one readable sentence.
    message = str(raised.value)
    assert "[640, 480]" in message
    assert "[999, 999]" in message
    assert "2026.09.29" in message


def test_payload_survives_a_process_boundary_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "model.stl"
    target.write_bytes(_binary_stl())
    expected = _expected(target)
    expected["sha256"] = "0" * 64

    with pytest.raises(wc.WriteVerificationError) as raised:
        wc.verify_artifact(
            "export_model", target, expected=expected, host_version="2021.01", params={"a": 1}
        )

    rebuilt = wc.WriteVerificationError.from_payload(dict(raised.value.payload))
    assert rebuilt.payload == raised.value.payload
    assert rebuilt.check == raised.value.check
    assert rebuilt.expected == raised.value.expected
    assert rebuilt.actual == raised.value.actual
    assert rebuilt.host_version == "2021.01"


# --------------------------------------------------------------------------
# Format readers
# --------------------------------------------------------------------------


def test_read_png_size_handles_non_png_and_short_files(tmp_path: Path) -> None:
    assert wc.read_png_size(tmp_path / "absent.png") is None
    short = tmp_path / "short.png"
    short.write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    assert wc.read_png_size(short) is None
    text = tmp_path / "text.png"
    text.write_bytes(b"not a png at all, really")
    assert wc.read_png_size(text) is None


def test_read_stl_shape_distinguishes_encodings(tmp_path: Path) -> None:
    binary = tmp_path / "a.stl"
    binary.write_bytes(_binary_stl(3))
    assert wc.read_stl_shape(binary) == {"encoding": "binary", "triangles": 3, "size": 234}

    ascii_stl = tmp_path / "b.stl"
    ascii_stl.write_text("solid x\nendsolid x\n", encoding="utf-8")
    shape = wc.read_stl_shape(ascii_stl)
    assert shape is not None
    assert shape["encoding"] == "ascii"
    assert shape["triangles"] is None


def test_read_stl_shape_handles_unreadable_files(tmp_path: Path) -> None:
    assert wc.read_stl_shape(tmp_path / "absent.stl") is None
    empty = tmp_path / "empty.stl"
    empty.write_bytes(b"\x00" * 10)
    assert wc.read_stl_shape(empty) is None


# --------------------------------------------------------------------------
# Comparison helpers
# --------------------------------------------------------------------------


def test_numbers_match_tolerates_float_noise_only() -> None:
    assert wc.numbers_match(1.0, 1.0)
    assert wc.numbers_match(1.0, 1.0000000001)
    assert not wc.numbers_match(1.0, 1.5)
    assert not wc.numbers_match(1.0, "not a number")
    assert not wc.numbers_match(None, None)


def test_sequences_match_rejects_length_mismatch() -> None:
    assert wc.sequences_match([1, 2, 3], [1, 2, 3])
    assert not wc.sequences_match([1, 2, 3], [1, 2])
    assert not wc.sequences_match([1, 2], [1, 2, 3])
    assert not wc.sequences_match([1, 2], ["a", "b"])


def test_jsonable_keeps_unserialisable_values_visible() -> None:
    assert wc.jsonable(float("nan")) == "nan"
    assert wc.jsonable(float("inf")) == "inf"
    assert wc.jsonable({"a": (1, 2)}) == {"a": [1, 2]}
    assert isinstance(wc.jsonable(object()), str)


def test_format_message_omits_absent_optional_parts() -> None:
    message = wc.format_message({"tool": "t", "check": "c", "expected": 1, "actual": 2})
    assert "host OpenSCAD" not in message
    assert "expected 1" in message and "read back 2" in message
