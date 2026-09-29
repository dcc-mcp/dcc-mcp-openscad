"""Post-write read-back contract: every mutating tool proves its own effect.

The failure mode under test is "reported success, artifact unchanged". OpenSCAD
is a separate CLI process and exits 0 on several paths that write nothing at
all -- an unrecognised output suffix, an empty geometry, a swallowed export --
so a green return code carries no information on its own.

Each mutating method therefore has a case where the host silently drops the
write, and the test asserts the tool refuses to return instead of handing the
caller a plausible-looking payload.
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import pytest

from dcc_mcp_openscad import write_contract
from dcc_mcp_openscad.bridge import OpenscadCli, OpenScadWriteVerificationError

HOST_VERSION = "2026.09.29"
HOST_MATRIX = {"status": "supported", "range": {"id": "2026.09.x"}}


# ---------------------------------------------------------------------------
# Contract primitives
# ---------------------------------------------------------------------------


def test_message_states_tool_check_and_both_sides():
    error = write_contract.WriteVerificationError(
        tool="export_model",
        check="artifact.sha256",
        expected="a" * 64,
        actual="b" * 64,
        host_version=HOST_VERSION,
    )

    message = str(error)

    assert "export_model" in message
    assert "artifact.sha256" in message
    assert "a" * 64 in message and "b" * 64 in message
    assert HOST_VERSION in message


def test_error_payload_round_trips_across_a_process_boundary():
    error = write_contract.WriteVerificationError(
        tool="render_preview",
        check="png.dimensions",
        expected=[640, 480],
        actual=[32, 32],
        host_version=HOST_VERSION,
        host_matrix=HOST_MATRIX,
        params={"output_path": "/tmp/model.png"},
    )

    revived = write_contract.WriteVerificationError.from_payload(
        json.loads(json.dumps(error.payload))
    )

    assert revived.payload == error.payload
    assert str(revived) == str(error)


def test_numbers_and_sequences_discriminate_real_differences():
    assert write_contract.numbers_match(84.0, 84.0 + 1e-12)
    assert not write_contract.numbers_match(84.0, 80.0)
    assert not write_contract.numbers_match(84.0, None)
    assert write_contract.sequences_match([1, 2, 3], (1.0, 2.0, 3.0))
    assert not write_contract.sequences_match([1, 2, 3], [1, 2])
    assert not write_contract.sequences_match([1, 2, 3], [1, 2, 4])


def test_every_skill_tool_is_classified_as_mutating_or_read_only(tmp_path: Path):
    """An unclassified tool has no answer to "does this owe a read-back?"."""
    import yaml

    from dcc_mcp_openscad.write_contract import (
        MUTATING_TOOLS,
        READ_ONLY_TOOLS,
        TOOL_CLASSIFICATION_ERROR,
    )

    root = Path(__file__).parents[1]
    tools_yaml = root / "src" / "dcc_mcp_openscad" / "skills" / "openscad-pipeline" / "tools.yaml"
    declared = {
        tool["name"] for tool in yaml.safe_load(tools_yaml.read_text(encoding="utf-8"))["tools"]
    }

    assert declared == set(MUTATING_TOOLS) | set(READ_ONLY_TOOLS), TOOL_CLASSIFICATION_ERROR
    assert not (set(MUTATING_TOOLS) & set(READ_ONLY_TOOLS))


def test_mutating_tools_are_the_ones_that_write_an_artifact():
    from dcc_mcp_openscad.write_contract import MUTATING_TOOLS, READ_ONLY_TOOLS

    for tool in ("export_model", "render_preview"):
        assert tool in MUTATING_TOOLS, tool
    # validate_model compiles into a private temporary directory that is removed
    # with the call, so nothing survives that a caller could depend on.
    for tool in ("get_status", "get_capabilities", "inspect_source", "validate_model"):
        assert tool in READ_ONLY_TOOLS, tool


# ---------------------------------------------------------------------------
# Artifact readers
# ---------------------------------------------------------------------------


def _binary_stl(path: Path, facets: int = 12) -> None:
    body = b"\x00" * (50 * facets)
    path.write_bytes(b"\x00" * 80 + struct.pack("<I", facets) + body)


def test_binary_stl_reader_reports_the_declared_facet_count(tmp_path: Path):
    path = tmp_path / "model.stl"
    _binary_stl(path, facets=7)

    assert write_contract.read_binary_stl(path) == 7
    assert write_contract.read_ascii_stl_facets(path) is None


def test_ascii_stl_reader_counts_facets(tmp_path: Path):
    path = tmp_path / "model.stl"
    path.write_text(
        "solid model\nfacet normal 0 0 0\nendfacet\nfacet normal 0 0 1\nendfacet\nendsolid\n",
        encoding="utf-8",
    )

    assert write_contract.read_binary_stl(path) is None
    assert write_contract.read_ascii_stl_facets(path) == 2


def test_png_reader_reports_ihdr_dimensions(tmp_path: Path):
    path = tmp_path / "preview.png"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + struct.pack(">II", 640, 480)
        + b"\x00" * 8
    )

    assert write_contract.read_png_dimensions(path) == [640, 480]


def test_readers_return_none_instead_of_raising_on_foreign_bytes(tmp_path: Path):
    path = tmp_path / "notes.txt"
    path.write_bytes(b"not an artifact")

    assert write_contract.read_png_dimensions(path) is None
    assert write_contract.read_binary_stl(path) is None
    assert write_contract.read_ascii_stl_facets(path) is None


# ---------------------------------------------------------------------------
# verify_artifact
# ---------------------------------------------------------------------------


def _mismatch(excinfo) -> write_contract.WriteVerificationError:
    """Assert the failure is a structured read-back mismatch and return it.

    Both sides of the comparison are required to be present, not merely absent:
    a mismatch that only names the tool is the "failed, go guess" report this
    contract exists to eliminate.
    """
    error = excinfo.value
    assert isinstance(error, write_contract.WriteVerificationError), type(error)
    assert error.tool, "the error must name the tool"
    assert error.check, "the error must name the check that disagreed"
    assert "expected" in error.payload and "actual" in error.payload
    assert error.host_version == HOST_VERSION, "the host version travels with the mismatch"
    return error


def _expected(path: Path, **extra) -> dict:
    payload = {
        "sha256": write_contract.sha256_file(path),
        "bytes": path.stat().st_size,
        "suffix": path.suffix.lower(),
    }
    payload.update(extra)
    return payload


def test_a_healthy_binary_stl_passes_every_read_back(tmp_path: Path):
    path = tmp_path / "model.stl"
    _binary_stl(path, facets=12)

    verified = write_contract.verify_artifact(
        "export_model",
        path,
        expected=_expected(path),
        host_version=HOST_VERSION,
        host_matrix=HOST_MATRIX,
    )

    for check in (
        "artifact.exists",
        "artifact.bytes",
        "artifact.non_empty",
        "artifact.sha256",
        "stl.facets",
        "stl.non_empty_geometry",
    ):
        assert check in verified, check


def test_a_missing_artifact_is_refused(tmp_path: Path):
    path = tmp_path / "missing.stl"

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "export_model",
            path,
            expected={"sha256": "0" * 64, "bytes": 100, "suffix": ".stl"},
            host_version=HOST_VERSION,
            host_matrix=HOST_MATRIX,
        )

    assert _mismatch(excinfo).check == "artifact.exists"


def test_an_empty_artifact_is_refused(tmp_path: Path):
    path = tmp_path / "empty.stl"
    path.write_bytes(b"")

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "export_model",
            path,
            expected={"sha256": "0" * 64, "bytes": 0, "suffix": ".stl"},
            host_version=HOST_VERSION,
            host_matrix=HOST_MATRIX,
        )

    assert _mismatch(excinfo).check == "artifact.non_empty"


def test_a_rewritten_artifact_is_refused(tmp_path: Path):
    """The classic silent drop: something else owns the bytes now."""
    path = tmp_path / "model.stl"
    _binary_stl(path, facets=12)
    expected = _expected(path)
    _binary_stl(path, facets=3)  # the file changes after the export recorded it

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "export_model", path, expected=expected, host_version=HOST_VERSION
        )

    error = _mismatch(excinfo)
    assert error.check == "artifact.bytes"
    assert error.expected != error.actual


def test_an_artifact_edited_in_place_is_caught_by_the_digest(tmp_path: Path):
    """Same length, different bytes: only the read-back digest can see it."""
    path = tmp_path / "model.stl"
    _binary_stl(path, facets=12)
    expected = _expected(path)
    with path.open("r+b") as stream:
        stream.seek(0)
        stream.write(b"\xff" * 80)  # header only: same size, different file

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "export_model", path, expected=expected, host_version=HOST_VERSION
        )

    error = _mismatch(excinfo)
    assert error.check == "artifact.sha256"
    assert error.expected != error.actual


def test_an_stl_with_no_geometry_is_refused(tmp_path: Path):
    """Exited 0, wrote a valid header, carried no model."""
    path = tmp_path / "empty-geometry.stl"
    _binary_stl(path, facets=0)

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "export_model",
            path,
            expected=_expected(path),
            host_version=HOST_VERSION,
        )

    error = _mismatch(excinfo)
    assert error.check == "stl.facets"
    assert error.actual == 0


def test_a_truncated_stl_is_refused_by_size_consistency(tmp_path: Path):
    path = tmp_path / "truncated.stl"
    _binary_stl(path, facets=40)
    path.write_bytes(path.read_bytes()[:-10])  # ten bytes short of 40 facets

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "export_model",
            path,
            expected={
                "sha256": write_contract.sha256_file(path),
                "bytes": path.stat().st_size,
                "suffix": ".stl",
            },
            host_version=HOST_VERSION,
        )

    error = _mismatch(excinfo)
    assert error.check == "stl.size_consistent"
    assert error.expected == 84 + 40 * 50
    assert error.actual == path.stat().st_size


def test_an_ascii_stl_with_no_facets_is_refused(tmp_path: Path):
    path = tmp_path / "empty-ascii.stl"
    path.write_text("solid model\nendsolid model\n", encoding="utf-8")

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "export_model",
            path,
            expected=_expected(path),
            host_version=HOST_VERSION,
        )

    assert _mismatch(excinfo).check == "stl.ascii_facets"


def test_a_png_that_is_not_a_png_is_refused(tmp_path: Path):
    path = tmp_path / "preview.png"
    path.write_bytes(b"<html>no GL available</html>")

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "render_preview",
            path,
            expected=_expected(path, width=640, height=480),
            host_version=HOST_VERSION,
        )

    assert _mismatch(excinfo).check == "png.header"


def test_a_png_of_the_wrong_frame_size_is_refused(tmp_path: Path):
    """A render that silently produced a different frame is a failed write."""
    path = tmp_path / "preview.png"
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\x0d"
        + b"IHDR"
        + struct.pack(">II", 32, 32)
        + b"\x00" * 8
    )

    with pytest.raises(write_contract.WriteVerificationError) as excinfo:
        write_contract.verify_artifact(
            "render_preview",
            path,
            expected=_expected(path, width=640, height=480),
            host_version=HOST_VERSION,
        )

    error = _mismatch(excinfo)
    assert error.check == "png.dimensions"
    assert error.expected == [640, 480]
    assert error.actual == [32, 32]


def test_other_formats_still_get_the_identity_read_back(tmp_path: Path):
    """The generic path proves the artifact, without claiming to parse it."""
    path = tmp_path / "model.3mf"
    path.write_bytes(b"<?xml version='1.0'?><model/>")

    verified = write_contract.verify_artifact(
        "export_model",
        path,
        expected=_expected(path),
        host_version=HOST_VERSION,
    )

    assert verified == [
        "artifact.exists",
        "artifact.bytes",
        "artifact.non_empty",
        "artifact.sha256",
    ]


# ---------------------------------------------------------------------------
# The bridge must actually wire the read-back into both mutating tools
# ---------------------------------------------------------------------------


class _DroppingOpenScad(OpenscadCli):
    """An OpenSCAD that exits 0 and leaves the artifact in a given state."""

    def __init__(self, root: Path, payload: bytes):
        super().__init__(allowed_roots=[root])
        self.executable = "fake-openscad"
        self.payload = payload

    def _run(self, args, timeout_secs, cwd=None):
        if "--version" in args:
            return self._result(stdout="OpenSCAD version %s\n" % HOST_VERSION)
        if "--help" in args:
            return self._result(stdout="--hardwarnings --render -p -P")
        if "-o" in args:
            output = Path(args[list(args).index("-o") + 1])
            output.write_bytes(self.payload)
        return self._result()

    @staticmethod
    def _result(stdout="", stderr="", returncode=0):
        return {
            "returncode": returncode,
            "duration_secs": 0.01,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "diagnostics": [],
        }


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "model.scad"
    source.write_text("cube(10);", encoding="utf-8")
    return source


def test_export_model_reports_the_checks_it_ran(tmp_path: Path):
    source = _source(tmp_path)
    payload = b"\x00" * 80 + struct.pack("<I", 12) + b"\x00" * (50 * 12)
    cli = _DroppingOpenScad(tmp_path, payload)

    result = cli.export_model(str(source), str(tmp_path / "model.stl"))

    for check in (
        "artifact.exists",
        "artifact.non_empty",
        "artifact.sha256",
        "stl.facets",
    ):
        assert check in result["verified"], check


def test_export_model_refuses_an_artifact_with_no_geometry(tmp_path: Path):
    """Exited 0, produced a valid STL header, carried no model."""
    source = _source(tmp_path)
    cli = _DroppingOpenScad(tmp_path, b"\x00" * 80 + struct.pack("<I", 0))
    output = tmp_path / "model.stl"

    with pytest.raises(OpenScadWriteVerificationError) as excinfo:
        cli.export_model(str(source), str(output))

    assert excinfo.value.verification["check"] == "stl.facets"
    assert excinfo.value.verification["host_version"] == HOST_VERSION
    assert "did not take effect" in str(excinfo.value)


def test_render_preview_refuses_a_frame_that_is_not_a_png(tmp_path: Path):
    """Non-empty bytes that are not a PNG: only the content read-back sees it."""
    source = _source(tmp_path)
    cli = _DroppingOpenScad(tmp_path, b"<html>no GL</html>")
    output = tmp_path / "preview.png"

    with pytest.raises(OpenScadWriteVerificationError) as excinfo:
        cli.render_preview(str(source), str(output))

    assert excinfo.value.verification["check"] == "png.header"


def test_a_read_back_failure_is_still_an_adapter_error(tmp_path: Path):
    """Callers that already handle OpenScadError keep working unchanged."""
    from dcc_mcp_openscad.bridge import OpenScadError

    source = _source(tmp_path)
    cli = _DroppingOpenScad(tmp_path, b"\x00" * 84)

    with pytest.raises(OpenScadError):
        cli.export_model(str(source), str(tmp_path / "model.stl"))

    assert os.path.isfile(str(tmp_path / "model.stl"))
