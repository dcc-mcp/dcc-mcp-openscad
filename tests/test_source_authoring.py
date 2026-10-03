import hashlib
import os
from types import SimpleNamespace

import pytest

from dcc_mcp_openscad import source_authoring as a


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        a, "get_bridge", lambda: SimpleNamespace(allowed_roots=(tmp_path.resolve(),))
    )
    return tmp_path


def test_exact_and_no_overwrite(root):
    p = root / "kit.scad"
    text = "// café\ncube([1,2,3]);\n"
    r = a.write_model_source(str(p), text)
    assert p.read_text(encoding="utf-8") == text and r["bytes"] == len(text.encode())
    assert r["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    with pytest.raises(FileExistsError):
        a.write_model_source(str(p), text)
    assert p.read_text(encoding="utf-8") == text


@pytest.mark.skipif(os.name != "nt", reason="Windows filename semantics")
@pytest.mark.parametrize(
    "filename",
    [
        "kept.scad:copy.scad",
        "NUL.scad",
        "CON.scad",
        "COM1.scad",
        "COM¹.scad",
        "LPT².scad",
        "CON .scad",
        "CONIN$.scad",
        "CONOUT$.scad",
        "folder./new.scad",
    ],
)
def test_windows_file_components_rejected(root, filename):
    kept = root / "kept.scad"
    kept.write_text("cube(2);", encoding="utf-8")
    with pytest.raises(ValueError, match="ordinary filename"):
        a.write_model_source(str(root / filename), "cube(3);")
    assert kept.read_text(encoding="utf-8") == "cube(2);"
    assert list(root.iterdir()) == [kept]


@pytest.mark.parametrize(
    "source",
    [
        "",
        "\x00",
        "include <../secret>",
        'import("x");',
        'surface(file="x");',
        "use <a.scad>",
        "x" * 262145,
    ],
    ids=["empty", "nul", "include", "import", "surface", "use", "utf8-budget"],
)
def test_reject_source(root, source):
    with pytest.raises(ValueError):
        a.write_model_source(str(root / "x.scad"), source)
    assert not (root / "x.scad").exists()


def test_reject_paths(root):
    for p in [
        "relative.scad",
        str(root / "../outside.scad"),
        str(root / "bad.txt"),
        str(root / "missing/x.scad"),
        str(root.parent / "outside.scad"),
    ]:
        with pytest.raises(ValueError):
            a.write_model_source(p, "cube(1);")
    try:
        (root / "link").symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not available to this test process")
    with pytest.raises(ValueError):
        a.write_model_source(str(root / "link/x.scad"), "cube(1);")


def test_core_result_envelope_preserves_verification_checks(monkeypatch):
    from dcc_mcp_openscad import skill_tools

    monkeypatch.setattr(
        skill_tools,
        "get_bridge",
        lambda: SimpleNamespace(
            export_model=lambda **kwargs: {
                "verified": ["artifact.exists", "artifact.sha256"],
                "bytes": 7,
            }
        ),
    )
    result = skill_tools.bridge_main("export_model", "ok")()
    assert result["success"] is True
    assert result["context"]["verification_checks"] == ["artifact.exists", "artifact.sha256"]
    assert result["postcondition"]["verified"] is True


def test_utf8_byte_limit_is_not_a_character_limit(root):
    text = "//" + "é" * 131071
    result = a.write_model_source(str(root / "limit.scad"), text)
    assert result["bytes"] == a.MAX_BYTES
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        a.write_model_source(str(root / "too-large.scad"), text + "é")
    assert not (root / "too-large.scad").exists()


def test_external_keyword_in_comment_is_deliberately_rejected(root):
    with pytest.raises(ValueError, match="external"):
        a.write_model_source(str(root / "comment.scad"), "// import is rejected\ncube(1);")
    assert not (root / "comment.scad").exists()


def test_symlink_target_remains_unchanged(root):
    target = root / "kept.scad"
    target.write_text("cube(2);", encoding="utf-8")
    alias = root / "alias.scad"
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is not available to this test process")
    with pytest.raises(ValueError, match="symlink"):
        a.write_model_source(str(alias), "cube(3);")
    assert target.read_text(encoding="utf-8") == "cube(2);"
