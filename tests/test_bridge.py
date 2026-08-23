from __future__ import annotations

import os
from pathlib import Path

import pytest

from dcc_mcp_openscad.bridge import (
    OpenscadCli,
    OpenScadError,
    _parameter_args,
)


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
            output.write_bytes(b"fake artifact")
        return self._result(stderr="Geometries in cache: 1\n")

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
    args = cli.calls[-1][0]
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
    assert result["bytes"] == len(b"fake artifact")
    assert len(result["sha256"]) == 64
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
    args = cli.calls[-1][0]
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
