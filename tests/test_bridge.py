from __future__ import annotations

import os
import struct
import zlib
from pathlib import Path

import pytest

from dcc_mcp_openscad.bridge import (
    OpenscadCli,
    OpenScadError,
    _parameter_args,
)


def last_export_call(cli: FakeOpenScad) -> list:
    """Last call that actually produced an artifact.

    The write contract probes ``--version`` for its evidence triple, so
    ``calls[-1]`` is not necessarily the export any more.
    """
    return next(args for args, _, _ in reversed(cli.calls) if "-o" in args)


def make_png(width: int, height: int) -> bytes:
    """Build a genuinely valid greyscale PNG so header read-backs are real."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes([(x + y) % 256 for x in range(width)]) for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_binary_stl(triangles: int = 2) -> bytes:
    """Build a binary STL whose declared triangle count matches its length."""
    body = b"".join(b"\x00" * 50 for _ in range(triangles))
    return b"fake-openscad binary stl".ljust(80, b"\x00") + struct.pack("<I", triangles) + body


class FakeOpenScad(OpenscadCli):
    def __init__(self, root: Path):
        super().__init__(allowed_roots=[root])
        self.executable = "fake-openscad"
        self.calls = []

    def _run(self, args, timeout_secs, cwd=None):
        self.calls.append((list(args), timeout_secs, cwd))
        if "--version" in args:
            return self._result(stdout="OpenSCAD version 2021.01\n")
        if "--help" in args:
            return self._result(
                stdout="--hardwarnings --check-parameters --check-parameter-ranges --render -p -P"
            )
        if "-o" in args:
            output = Path(args[list(args).index("-o") + 1])
            self._write_artifact(output, args)
        return self._result(stderr="Geometries in cache: 1\n")

    @staticmethod
    def _write_artifact(output: Path, args: list) -> None:
        """Write the artifact shape a real host would, honouring the flags.

        The post-write contract reads these files back, so the fixture has to
        produce the same headers OpenSCAD produces; a fixed byte string would
        only prove the contract rejects nonsense.
        """
        if output.suffix.lower() == ".png":
            width, height = 1200, 800
            if "--imgsize" in args:
                size = args[args.index("--imgsize") + 1].split(",")
                width, height = int(size[0]), int(size[1])
            output.write_bytes(make_png(width, height))
            return
        if output.suffix.lower() == ".stl":
            if "asciistl" in args:
                output.write_text(
                    "solid fake\nfacet normal 0 0 0\nendsolid fake\n", encoding="utf-8"
                )
                return
            output.write_bytes(make_binary_stl())
            return
        output.write_bytes(b"fake artifact")

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


def test_inspect_file_ignores_comments_and_strings(tmp_path: Path):
    source = tmp_path / "sample.scad"
    source.write_text(
        "// module fake() {}\n"
        'label = "function fake() include <fake.scad>";\n'
        "module cube_part(size=10) { cube(size); }\n"
        "function doubled(value) = value * 2;\n"
        "include <shared.scad>\n",
        encoding="utf-8",
    )
    (tmp_path / "shared.scad").write_text("shared = true;", encoding="utf-8")

    result = OpenscadCli(allowed_roots=[tmp_path]).inspect_file(str(source))

    assert result["modules"] == ["cube_part"]
    assert result["functions"] == ["doubled"]
    assert result["assigned_variables"] == ["label"]
    assert result["dependencies"] == [
        {
            "kind": "include",
            "reference": "shared.scad",
            "local_path": str((tmp_path / "shared.scad").resolve()),
            "local_exists": True,
        }
    ]


def test_source_must_stay_inside_allowed_roots(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.scad"
    outside.write_text("cube(1);", encoding="utf-8")

    with pytest.raises(OpenScadError, match="outside"):
        OpenscadCli(allowed_roots=[allowed]).inspect_file(str(outside))


def test_parameter_overrides_are_typed_and_deterministic():
    args = _parameter_args(
        {"name": 'quote " and newline\n', "enabled": True, "size": [1, 2.5, None]}
    )

    assert args == [
        "-D",
        "enabled=true",
        "-D",
        'name="quote \\" and newline\\n"',
        "-D",
        "size=[1, 2.5, undef]",
    ]

    with pytest.raises(OpenScadError, match="Invalid"):
        _parameter_args({'size); system("bad")': 1})
    with pytest.raises(OpenScadError, match="support only"):
        _parameter_args({"size": {"raw": "expression"}})


def test_headless_host_that_prints_nothing_is_not_ready(tmp_path: Path):
    """A Qt GUI openscad with no DISPLAY exits 0 having printed nothing.

    That used to raise IndexError on `splitlines()[0]`; it must instead report
    an unversioned host that is not ready, so callers fail closed instead of
    crashing inside the status probe.
    """

    class SilentHost(OpenscadCli):
        def __init__(self, root):
            super().__init__(allowed_roots=[root])
            self.executable = "silent-openscad"

        def _run(self, args, timeout_secs, cwd=None):
            return {
                "returncode": 0,
                "duration_secs": 0.01,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "diagnostics": [],
            }

    status = SilentHost(tmp_path).status()

    assert status["ready"] is False
    assert status["version"] == ""


def test_status_and_capabilities_are_version_probed(tmp_path: Path):
    cli = FakeOpenScad(tmp_path)

    status = cli.status()
    capabilities = cli.capabilities()

    assert status["ready"] is True
    assert status["version"] == "2021.01"
    assert status["instance_type"] == "standalone"
    assert capabilities["flags"]["--hardwarnings"] is True
    assert capabilities["flags"]["--summary-file"] is False


def test_validate_model_compiles_only_to_temporary_output(tmp_path: Path):
    source = tmp_path / "model.scad"
    source.write_text("size = 2; cube(size);", encoding="utf-8")
    cli = FakeOpenScad(tmp_path)

    result = cli.validate_model(str(source), parameters={"size": 4}, strict=True)

    assert result["valid"] is True
    assert result["compiled_bytes"] > 0
    args = last_export_call(cli)
    assert args[:5] == [
        "--hardwarnings",
        "--check-parameters",
        "true",
        "--check-parameter-ranges",
        "true",
    ]
    assert "size=4" in args


def test_export_is_atomic_and_refuses_implicit_overwrite(tmp_path: Path):
    source = tmp_path / "model.scad"
    output = tmp_path / "model.stl"
    source.write_text("cube(2);", encoding="utf-8")
    output.write_bytes(b"existing")
    cli = FakeOpenScad(tmp_path)

    with pytest.raises(OpenScadError, match="overwrite=true"):
        cli.export_model(str(source), str(output))

    result = cli.export_model(str(source), str(output), overwrite=True)

    assert result["overwritten"] is True
    # The fixture writes a real binary STL header plus two triangles.
    assert result["bytes"] == 84 + 2 * 50
    assert len(result["sha256"]) == 64
    assert result["read_back"]["sha256"] == result["sha256"]
    assert result["read_back"]["size"] == result["bytes"]
    assert result["read_back"]["stl_encoding"] == "binary"
    assert not list(tmp_path.glob(".model.*.stl"))


def test_render_preview_validates_png_contract(tmp_path: Path):
    source = tmp_path / "model.scad"
    source.write_text("cube(2);", encoding="utf-8")
    cli = FakeOpenScad(tmp_path)

    result = cli.render_preview(
        str(source),
        str(tmp_path / "model.png"),
        width=640,
        height=480,
        projection="orthographic",
    )

    assert result["width"] == 640
    assert result["projection"] == "orthographic"
    assert result["read_back"]["png_dimensions"] == [640, 480]
    args = last_export_call(cli)
    assert ["--imgsize", "640,480"] == args[0:2]

    with pytest.raises(OpenScadError, match="between 64 and 8192"):
        cli.render_preview(str(source), str(tmp_path / "bad.png"), width=16)


def _real_openscad() -> str:
    return os.environ.get("OPENSCAD_TEST_EXECUTABLE", "")


@pytest.mark.openscad
@pytest.mark.skipif(not _real_openscad(), reason="OPENSCAD_TEST_EXECUTABLE is not set")
def test_real_openscad_validate_export_and_render(tmp_path: Path):
    root = Path(__file__).parents[1]
    source = root / "examples" / "production_smoke.scad"
    cli = OpenscadCli(_real_openscad(), allowed_roots=[root, tmp_path])

    assert cli.status()["ready"] is True
    validation = cli.validate_model(str(source), parameters={"width": 90, "vent_count": 4})
    stl = cli.export_model(
        str(source),
        str(tmp_path / "production-smoke.stl"),
        parameters={"width": 90, "vent_count": 4},
    )
    png = cli.render_preview(
        str(source),
        str(tmp_path / "production-smoke.png"),
        width=640,
        height=480,
        full_render=True,
        parameters={"width": 90, "vent_count": 4},
    )

    assert validation["valid"] is True
    assert stl["bytes"] > 84 and len(stl["sha256"]) == 64
    assert png["bytes"] > 100 and len(png["sha256"]) == 64
