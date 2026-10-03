import hashlib
import os
import stat
from pathlib import Path
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


@pytest.mark.parametrize("module", ["import_stl", "import_dxf", "import_off", "IMPORT_STL"])
def test_legacy_external_imports_are_rejected_before_creation(root, module):
    with pytest.raises(ValueError, match="external"):
        a.write_model_source(str(root / "legacy.scad"), module + '("external.mesh");')
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("failure", ["write", "flush", "fsync", "read", "mismatch"])
def test_failed_staging_publishes_nothing_and_can_be_retried(root, monkeypatch, failure):
    destination = root / "retry.scad"
    source = "cube([1,2,3]);\n"
    real_fdopen = a.os.fdopen

    class FaultyStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def write(self, data):
            if failure == "write":
                self.stream.write(data[:3])
                raise OSError("synthetic partial write failure")
            return self.stream.write(data)

        def flush(self):
            if failure == "flush":
                raise OSError("synthetic flush failure")
            return self.stream.flush()

        def fileno(self):
            return self.stream.fileno()

        def seek(self, offset):
            return self.stream.seek(offset)

        def read(self):
            if failure == "read":
                raise OSError("synthetic readback failure")
            if failure == "mismatch":
                return b"different bytes"
            return self.stream.read()

    def fail_fsync(_fd):
        raise OSError("synthetic fsync failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(a.os, "fdopen", lambda *args: FaultyStream(real_fdopen(*args)))
        if failure == "fsync":
            scoped.setattr(a.os, "fsync", fail_fsync)
        error = RuntimeError if failure == "mismatch" else OSError
        with pytest.raises(error):
            a.write_model_source(str(destination), source)
    assert list(root.iterdir()) == []
    result = a.write_model_source(str(destination), source)
    assert destination.read_bytes() == source.encode("utf-8")
    assert result["sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert list(root.iterdir()) == [destination]


def test_fdopen_failure_closes_descriptor_and_removes_owned_stage(root, monkeypatch):
    real_mkstemp = a.tempfile.mkstemp
    created = []

    def capture_stage(*args, **kwargs):
        result = real_mkstemp(*args, **kwargs)
        created.append(result)
        return result

    def fail_fdopen(*_args):
        raise OSError("synthetic stream creation failure")

    monkeypatch.setattr(a.tempfile, "mkstemp", capture_stage)
    monkeypatch.setattr(a.os, "fdopen", fail_fdopen)
    with pytest.raises(OSError, match="stream creation"):
        a.write_model_source(str(root / "unpublished.scad"), "cube(1);")
    with pytest.raises(OSError):
        os.fstat(created[0][0])
    assert list(root.iterdir()) == []


def test_destination_created_during_publication_is_preserved(root, monkeypatch):
    destination = root / "concurrent.scad"
    replacement = b"cube(9);"
    real_link = a.os.link

    def create_before_link(stage, target):
        assert not destination.exists()
        destination.write_bytes(replacement)
        return real_link(stage, target)

    monkeypatch.setattr(a.os, "link", create_before_link)
    with pytest.raises(FileExistsError):
        a.write_model_source(str(destination), "cube(1);")
    assert destination.read_bytes() == replacement
    assert list(root.iterdir()) == [destination]


def test_destination_replaced_after_link_is_not_reported_verified(root, monkeypatch):
    destination = root / "published.scad"
    replacement = root / "replacement"
    replacement.write_bytes(b"preserve replacement")
    real_link = a.os.link

    def replace_after_link(stage, target):
        real_link(stage, target)
        os.replace(str(replacement), str(destination))

    monkeypatch.setattr(a.os, "link", replace_after_link)
    with pytest.raises(RuntimeError, match="published file identity changed"):
        a.write_model_source(str(destination), "cube(1);")
    assert destination.read_bytes() == b"preserve replacement"
    assert list(root.iterdir()) == [destination]


@pytest.mark.parametrize("failure_point", ["fsync", "cleanup-after-publication"])
def test_staging_cleanup_error_preserves_original_outcome(root, monkeypatch, failure_point):
    destination = root / "published.scad"
    source = b"cube(1);"
    real_cleanup = a._remove_created_file
    retained = []

    def refuse_cleanup(stage, identity):
        retained.append((stage, identity))
        raise PermissionError("synthetic temporary cleanup failure")

    def fail_fsync(_fd):
        raise OSError("synthetic original fsync failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(a, "_remove_created_file", refuse_cleanup)
        if failure_point == "fsync":
            scoped.setattr(a.os, "fsync", fail_fsync)
            with pytest.raises(OSError, match="original fsync failure"):
                a.write_model_source(str(destination), source.decode("utf-8"))
            assert not destination.exists()
        else:
            result = a.write_model_source(str(destination), source.decode("utf-8"))
            assert result["sha256"] == hashlib.sha256(source).hexdigest()
            assert destination.read_bytes() == source
    assert len(retained) == 1 and retained[0][0].exists()
    real_cleanup(*retained[0])
    assert list(root.iterdir()) == ([destination] if destination.exists() else [])
    if destination.exists():
        assert destination.read_bytes() == source


@pytest.mark.parametrize("replacement_point", ["validation", "link"])
def test_replaced_staging_file_is_preserved(root, monkeypatch, replacement_point):
    replacement = root / "replacement"
    replacement.write_bytes(b"preserve this separate file")
    observed = []
    real_validate = a._validated_destination

    def replace_stage(stage):
        before = a._file_identity(stage.lstat())
        os.replace(str(replacement), str(stage))
        assert a._file_identity(stage.lstat()) != before
        observed.append(stage)

    def replace_during_validation(raw):
        stages = list(root.glob(".dcc-mcp-scad-*"))
        if stages:
            replace_stage(stages[0])
        return real_validate(raw)

    def replace_during_link(stage, _target):
        replace_stage(Path(stage))
        raise OSError("synthetic publication failure")

    if replacement_point == "validation":
        monkeypatch.setattr(a, "_validated_destination", replace_during_validation)
        error = RuntimeError
    else:
        monkeypatch.setattr(a.os, "link", replace_during_link)
        error = OSError
    with pytest.raises(error):
        a.write_model_source(str(root / "unpublished.scad"), "cube(1);")
    assert len(observed) == 1
    assert observed[0].read_bytes() == b"preserve this separate file"
    assert list(root.iterdir()) == observed


def test_final_workspace_root_check_prevents_publication(root, monkeypatch):
    changed_root = root / "changed-root"
    changed_root.mkdir()
    real_fsync = a.os.fsync

    def change_roots_after_write(fd):
        real_fsync(fd)
        monkeypatch.setattr(a, "get_bridge", lambda: SimpleNamespace(allowed_roots=(changed_root,)))

    monkeypatch.setattr(a.os, "fsync", change_roots_after_write)
    with pytest.raises(ValueError, match="outside configured workspace"):
        a.write_model_source(str(root / "unpublished.scad"), "cube(1);")
    assert list(root.iterdir()) == [changed_root]


def test_reparse_parent_is_rejected_before_staging(root, monkeypatch):
    original_lstat = Path.lstat

    def reparse_root(path):
        if path == root:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", reparse_root)
    with pytest.raises(ValueError, match="reparse"):
        a.write_model_source(str(root / "unpublished.scad"), "cube(1);")
    assert list(root.iterdir()) == []


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
