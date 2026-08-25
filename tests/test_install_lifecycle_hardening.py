from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


def _schema_validate(payload: dict) -> None:
    from dcc_mcp_core.deployment import load_install_sop_schema
    from jsonschema import Draft202012Validator

    Draft202012Validator(load_install_sop_schema()).validate(payload)


def _fake_openscad(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import dcc_mcp_openscad.doctor as lifecycle

    executable = tmp_path / ("openscad.com" if os.name == "nt" else "openscad")
    executable.write_bytes(b"official-openscad-fixture-v1")

    def fake_run(command, *, timeout, cwd=None, env=None):
        del cwd, env
        assert 0 < timeout <= 5
        if "--version" in command:
            return {
                "success": True,
                "returncode": 0,
                "stdout": "OpenSCAD version 2024.01.15\n",
                "stderr": "",
                "truncated": False,
            }
        if "--help" in command:
            return {
                "success": True,
                "returncode": 0,
                "stdout": "--hardwarnings --render -p -P\n",
                "stderr": "",
                "truncated": False,
            }
        raise AssertionError("unexpected OpenSCAD probe")

    monkeypatch.setattr(lifecycle, "_run_bounded_command", fake_run)
    return executable


def _run_json(arguments: list[str], capsys) -> tuple[int, dict]:
    from dcc_mcp_openscad import server

    exit_code = server.main(arguments)
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    _schema_validate(payload)
    return exit_code, payload


def test_uses_only_the_official_core_02014_install_contract() -> None:
    from dcc_mcp_core.deployment import load_install_sop_schema

    import dcc_mcp_openscad.doctor as lifecycle

    assert lifecycle.MINIMUM_CORE_VERSION == "0.20.14"
    assert lifecycle.load_install_sop_schema() == load_install_sop_schema()
    assert lifecycle.SCHEMA_VERSION == 1
    assert lifecycle.LIFECYCLE_COMMANDS == frozenset(
        {"doctor", "install", "status", "verify", "uninstall", "upgrade"}
    )
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert 'dependencies=["dcc-mcp-core>=0.20.14,<1.0.0"]' in pyproject.read_text(encoding="utf-8")


def test_core_identity_binds_the_resolved_managed_python_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    real_python = Path(sys.executable).resolve(strict=True)
    managed_entrypoint = tmp_path / ("python.exe" if os.name == "nt" else "python")
    managed_entrypoint.symlink_to(real_python)
    monkeypatch.setattr(lifecycle.sys, "executable", str(managed_entrypoint))

    identity = lifecycle._capture_core_identity()

    assert identity["python"]["path"] == str(real_python)


def test_every_lifecycle_verb_emits_the_official_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipts" / "openscad.json"

    for command in ("doctor", "install", "status", "verify", "uninstall", "upgrade"):
        arguments = [
            command,
            "--json",
            "--dry-run",
            "--executable",
            str(executable),
            "--receipt-path",
            str(receipt),
            "--timeout-secs",
            "5",
        ]
        exit_code, payload = _run_json(arguments, capsys)
        assert exit_code in {0, 10, 40}
        assert payload["schema_version"] == 1
        assert payload["dcc_type"] == "openscad"
        assert payload["command"] == command
        assert payload["receipt_path"] in {None, "configured"}


def test_install_uses_one_caller_entry_deadline_across_both_identity_captures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    executable = tmp_path / ("openscad.com" if os.name == "nt" else "openscad")
    executable.write_bytes(b"deadline-fixture")
    observed: list[float] = []

    def fake_run(command, *, timeout, cwd=None, env=None):
        del cwd, env
        observed.append(timeout)
        time.sleep(0.03)
        output = (
            "OpenSCAD version 2024.01\n"
            if "--version" in command
            else "--hardwarnings --render -p -P\n"
        )
        return {
            "success": True,
            "returncode": 0,
            "stdout": output,
            "stderr": "",
            "truncated": False,
        }

    monkeypatch.setattr(lifecycle, "_run_bounded_command", fake_run)
    exit_code, payload = _run_json(
        [
            "install",
            "--yes",
            "--json",
            "--executable",
            str(executable),
            "--receipt-path",
            str(tmp_path / "receipt.json"),
            "--timeout-secs",
            "1",
        ],
        capsys,
    )

    assert exit_code == 0
    assert payload["verify"]["directly_usable"] is True
    assert len(observed) == 4
    assert all(later < earlier for earlier, later in zip(observed, observed[1:])), observed


def test_install_verify_uninstall_are_receipted_owned_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipts" / "openscad.json"
    common = [
        "--json",
        "--executable",
        str(executable),
        "--receipt-path",
        str(receipt),
        "--timeout-secs",
        "5",
    ]

    exit_code, plan = _run_json(["install", "--dry-run", *common], capsys)
    assert exit_code == 0
    assert plan["status"] == "planned"
    assert not receipt.exists()

    exit_code, installed = _run_json(["install", "--yes", *common], capsys)
    assert exit_code == 0
    assert installed["status"] == "ok"
    local_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    assert local_receipt["receipt_version"] == 1
    assert local_receipt["dcc_type"] == "openscad"
    assert local_receipt["openscad"]["sha256"]
    assert local_receipt["python"]["sha256"]
    assert local_receipt["core"]["version"] >= "0.20.14"

    exit_code, verified = _run_json(["verify", *common], capsys)
    assert exit_code == 0
    assert verified["verify"]["directly_usable"] is True

    exit_code, removed = _run_json(["uninstall", "--yes", *common], capsys)
    assert exit_code == 0
    assert removed["status"] == "ok"
    assert not receipt.exists()
    exit_code, repeated = _run_json(["uninstall", "--yes", *common], capsys)
    assert exit_code == 0
    assert repeated["steps"][0]["status"] == "skipped"


def test_install_cannot_replace_a_changed_owned_receipt_without_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipt.json"
    common = [
        "--json",
        "--executable",
        str(executable),
        "--receipt-path",
        str(receipt),
        "--timeout-secs",
        "5",
    ]
    assert _run_json(["install", "--yes", *common], capsys)[0] == 0
    previous = receipt.read_bytes()
    executable.write_bytes(b"official-openscad-fixture-v2")

    exit_code, refused = _run_json(["install", "--yes", *common], capsys)

    assert exit_code == 30
    assert refused["verify"]["failure_stage"] == "receipt"
    assert refused["verify"]["failure_reason"] == "upgrade_required"
    assert receipt.read_bytes() == previous


def test_runtime_identity_swap_and_symlink_fail_before_receipt_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipt.json"
    original_capture = lifecycle._capture_file_identity
    calls = 0

    def swap_after_probe(path: Path):
        nonlocal calls
        identity = original_capture(path)
        if Path(path).absolute() == executable.absolute():
            calls += 1
        # Selection performs one lexical identity check; mutate only when the
        # version probe performs its fresh post-I/O recapture.
        if Path(path).absolute() == executable.absolute() and calls == 3:
            executable.write_bytes(b"foreign-runtime-after-probe")
            identity = original_capture(path)
        return identity

    monkeypatch.setattr(lifecycle, "_capture_file_identity", swap_after_probe)
    exit_code, payload = _run_json(
        [
            "install",
            "--yes",
            "--json",
            "--executable",
            str(executable),
            "--receipt-path",
            str(receipt),
            "--timeout-secs",
            "5",
        ],
        capsys,
    )
    assert exit_code == 40
    assert payload["verify"]["failure_reason"] == "runtime_identity_changed"
    assert not receipt.exists()

    target = tmp_path / "real-openscad"
    target.write_bytes(b"official-openscad-fixture-v2")
    alias = tmp_path / "alias-openscad"
    alias.symlink_to(target)
    with pytest.raises(lifecycle.LifecycleFailure, match="reparse"):
        lifecycle._capture_file_identity(alias)


def test_receipt_commit_failure_rolls_back_the_prior_owned_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipt.json"
    common = [
        "--json",
        "--executable",
        str(executable),
        "--receipt-path",
        str(receipt),
        "--timeout-secs",
        "5",
    ]
    assert _run_json(["install", "--yes", *common], capsys)[0] == 0
    previous = receipt.read_bytes()
    executable.write_bytes(b"official-openscad-fixture-v2")
    original_replace = lifecycle._replace_path

    def fail_staged_commit(source: Path, destination: Path) -> None:
        if destination == receipt.resolve() and ".stage-" in source.name:
            raise OSError("private path C:/secret/token.txt")
        original_replace(source, destination)

    monkeypatch.setattr(lifecycle, "_replace_path", fail_staged_commit)
    exit_code, payload = _run_json(["upgrade", "--yes", *common], capsys)

    assert exit_code == 30
    assert payload["verify"]["failure_reason"] == "receipt_commit_failed"
    assert receipt.read_bytes() == previous
    serialized = json.dumps(payload)
    assert "secret" not in serialized
    assert "token.txt" not in serialized


def test_post_commit_validation_failure_restores_the_prior_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipt.json"
    common = [
        "--json",
        "--executable",
        str(executable),
        "--receipt-path",
        str(receipt),
        "--timeout-secs",
        "5",
    ]
    assert _run_json(["install", "--yes", *common], capsys)[0] == 0
    previous = receipt.read_bytes()
    executable.write_bytes(b"official-openscad-fixture-v3")
    new_sha = lifecycle._capture_file_identity(executable)["sha256"]
    original_read = lifecycle._read_receipt

    def corrupt_only_the_new_commit(path: Path):
        value = original_read(path)
        if value is not None and value["openscad"]["sha256"] == new_sha:
            value = dict(value)
            value["adapter_version"] = "9.9.9"
        return value

    monkeypatch.setattr(lifecycle, "_read_receipt", corrupt_only_the_new_commit)
    exit_code, payload = _run_json(["upgrade", "--yes", *common], capsys)

    assert exit_code == 30
    assert payload["verify"]["failure_reason"] == "receipt_commit_invalid"
    assert receipt.read_bytes() == previous


def test_malformed_foreign_receipts_and_reparse_parents_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipt.json"
    common = [
        "--json",
        "--executable",
        str(executable),
        "--receipt-path",
        str(receipt),
        "--timeout-secs",
        "5",
    ]
    assert _run_json(["install", "--yes", *common], capsys)[0] == 0
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["openscad"]["product"] = "ForeignCAD"
    payload["openscad"]["raw_secret"] = "token-value"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    exit_code, failed = _run_json(["status", *common], capsys)
    assert exit_code == 10
    assert failed["verify"]["failure_reason"] == "receipt_openscad_invalid"
    assert "ForeignCAD" not in json.dumps(failed)
    assert "token-value" not in json.dumps(failed)

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    linked_executable = linked_parent / "openscad"
    (real_parent / "openscad").write_bytes(b"runtime")
    with pytest.raises(lifecycle.LifecycleFailure, match="reparse"):
        lifecycle._capture_file_identity(linked_executable)
    with pytest.raises(lifecycle.LifecycleFailure, match="reparse"):
        lifecycle._assert_safe_receipt_parent(linked_parent / "receipt.json")

    dangling_receipt = tmp_path / "dangling-receipt.json"
    dangling_receipt.symlink_to(tmp_path / "missing-receipt.json")
    with pytest.raises(lifecycle.LifecycleFailure, match="reparse"):
        lifecycle._read_receipt(dangling_receipt)


def test_receipt_identity_change_during_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    executable = _fake_openscad(tmp_path, monkeypatch)
    receipt = tmp_path / "receipt.json"
    common = [
        "--json",
        "--executable",
        str(executable),
        "--receipt-path",
        str(receipt),
        "--timeout-secs",
        "5",
    ]
    assert _run_json(["install", "--yes", *common], capsys)[0] == 0
    original_read_text = Path.read_text
    changed = False

    def replace_after_read(path: Path, *args, **kwargs):
        nonlocal changed
        value = original_read_text(path, *args, **kwargs)
        if path == receipt and not changed:
            changed = True
            receipt.write_bytes(receipt.read_bytes() + b" ")
        return value

    monkeypatch.setattr(Path, "read_text", replace_after_read)
    exit_code, failed = _run_json(["status", *common], capsys)

    assert exit_code == 40
    assert failed["verify"]["failure_stage"] == "receipt"
    assert failed["verify"]["failure_reason"] == "receipt_identity_changed"


def test_receipt_mutations_use_one_nonblocking_owner_lock(tmp_path: Path) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    receipt = tmp_path / "receipt.json"
    with lifecycle._mutation_lock(receipt):
        with pytest.raises(lifecycle.LifecycleFailure) as raised:
            with lifecycle._mutation_lock(receipt):
                raise AssertionError("a second mutation must never enter")
        assert raised.value.exit_code == 20
        assert raised.value.stage == "lock"
        assert raised.value.reason == "lifecycle_lock_busy"

    with lifecycle._mutation_lock(receipt):
        pass


def test_malformed_core_schema_and_version_cli_are_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle
    from dcc_mcp_openscad import server
    from dcc_mcp_openscad.__version__ import __version__

    executable = _fake_openscad(tmp_path, monkeypatch)
    monkeypatch.setattr(lifecycle, "load_install_sop_schema", lambda: {"type": "object"})
    exit_code = server.main(["doctor", "--json", "--executable", str(executable)])
    failed = json.loads(capsys.readouterr().out)
    assert exit_code == 10
    assert failed["verify"] == {
        "directly_usable": False,
        "failure_stage": "schema",
        "failure_reason": "core_schema_invalid",
    }

    assert server.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_invalid_arguments_and_host_failures_are_stable_redacted_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    exit_code, malformed = _run_json(["verify", "--json", "--timeout-secs", "not-a-number"], capsys)
    assert exit_code == 10
    assert malformed["verify"]["failure_stage"] == "arguments"
    assert "usage:" not in json.dumps(malformed).lower()

    executable = tmp_path / "openscad"
    executable.write_bytes(b"fixture")

    def fail_probe(*_args, **_kwargs):
        raise RuntimeError("C:/private/operator/token=super-secret")

    monkeypatch.setattr(lifecycle, "_run_bounded_command", fail_probe)
    exit_code, failed = _run_json(
        ["doctor", "--json", "--executable", str(executable), "--timeout-secs", "1"], capsys
    )
    assert exit_code == 40
    serialized = json.dumps(failed)
    assert "private" not in serialized
    assert "super-secret" not in serialized
    assert "RuntimeError" not in serialized


def test_nonfinite_timeout_is_rejected_before_runtime_io(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import dcc_mcp_openscad.doctor as lifecycle

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError("runtime I/O must not run")

    monkeypatch.setattr(lifecycle, "_run_bounded_command", unexpected_probe)
    exit_code, failed = _run_json(["doctor", "--json", "--timeout-secs", "nan"], capsys)

    assert exit_code == 10
    assert failed["verify"]["failure_stage"] == "arguments"
    assert failed["verify"]["failure_reason"] == "timeout_invalid"


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def test_probe_timeout_owns_the_full_process_tree_and_leaves_no_orphan(tmp_path: Path) -> None:
    from dcc_mcp_openscad._process import run_bounded_command

    identities = tmp_path / "owned-tree.txt"
    ready = tmp_path / "descendant-ready.txt"
    identities_text = identities.as_posix()
    ready_text = ready.as_posix()
    descendant = (
        "import os,pathlib,time; "
        f"pathlib.Path({ready_text!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)"
    )
    root = (
        "import os,pathlib,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
        f"pathlib.Path({identities_text!r}).write_text("
        "str(os.getpid())+' '+str(child.pid), encoding='utf-8'); "
        f"deadline=time.monotonic()+3; "
        f'exec("while not pathlib.Path({ready_text!r}).is_file() '
        'and time.monotonic()<deadline: time.sleep(0.01)"); '
        "time.sleep(60)"
    )

    outcome = run_bounded_command([sys.executable, "-c", root], timeout=1.0)

    assert outcome == {
        "success": False,
        "reason": "probe_timeout",
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "truncated": False,
    }
    assert ready.is_file()
    root_pid, child_pid = (int(value) for value in identities.read_text().split())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (_pid_alive(root_pid) or _pid_alive(child_pid)):
        time.sleep(0.02)
    assert not _pid_alive(root_pid)
    assert not _pid_alive(child_pid)


def test_probe_cancellation_cleans_tree_and_private_directory_before_reraising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dcc_mcp_openscad._process as process_module

    identities = tmp_path / "cancelled-tree.txt"
    ready = tmp_path / "cancelled-ready.txt"
    private_root = tmp_path / "private-probe-root"
    descendant = (
        "import os,pathlib,time; "
        f"pathlib.Path({ready.as_posix()!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "time.sleep(60)"
    )
    root = (
        "import os,pathlib,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
        f"pathlib.Path({identities.as_posix()!r}).write_text("
        "str(os.getpid())+' '+str(child.pid), encoding='utf-8'); "
        "time.sleep(60)"
    )
    cancellation = KeyboardInterrupt("private cancellation detail")

    def controlled_root(prefix: str) -> str:
        assert prefix == "dcc-mcp-openscad-probe-"
        private_root.mkdir()
        return str(private_root)

    def cancel_after_ready() -> None:
        if ready.is_file():
            raise cancellation

    monkeypatch.setattr(process_module.tempfile, "mkdtemp", controlled_root)
    monkeypatch.setattr(process_module, "check_dcc_cancelled", cancel_after_ready)

    with pytest.raises(KeyboardInterrupt) as raised:
        process_module.run_bounded_command([sys.executable, "-c", root], timeout=5.0)

    assert raised.value is cancellation
    assert not private_root.exists()
    root_pid, child_pid = (int(value) for value in identities.read_text().split())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (_pid_alive(root_pid) or _pid_alive(child_pid)):
        time.sleep(0.02)
    assert not _pid_alive(root_pid)
    assert not _pid_alive(child_pid)


def test_probe_supervisor_ignores_hostile_python_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dcc_mcp_openscad._process as process_module

    marker = tmp_path / "sitecustomize-ran.txt"
    (tmp_path / "sitecustomize.py").write_text(
        "from pathlib import Path\nPath(%r).write_text('ran', encoding='utf-8')\n" % str(marker),
        encoding="utf-8",
    )
    captured: list[str] = []
    original_start = process_module._start_owned_process

    def capture_start(command, *, env, cwd):
        captured.extend(command)
        return original_start(command, env=env, cwd=cwd)

    monkeypatch.setattr(process_module, "_start_owned_process", capture_start)
    outcome = process_module.run_bounded_command(
        [sys.executable, "-I", "-S", "-c", "print('ok')"],
        timeout=3.0,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )

    assert outcome["success"] is True
    assert captured[:3] == [sys.executable, "-I", "-S"]
    assert not marker.exists()


def test_status_and_capabilities_share_one_caller_deadline(tmp_path: Path) -> None:
    from dcc_mcp_openscad.bridge import OpenscadCli

    executable = tmp_path / "openscad"
    executable.write_bytes(b"fixture")

    class RecordingCli(OpenscadCli):
        def __init__(self) -> None:
            self.executable = str(executable)
            self.allowed_roots = (tmp_path,)
            self.max_source_bytes = 1024
            self.max_timeout_secs = 1.0
            self.timeouts: list[float] = []

        def _run(self, args, timeout_secs, cwd=None):
            del cwd
            self.timeouts.append(timeout_secs)
            time.sleep(0.02)
            output = "OpenSCAD version 2024.01\n" if "--version" in args else "--render -p -P\n"
            return {
                "returncode": 0,
                "stdout": output,
                "stderr": "",
                "diagnostics": [],
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

    cli = RecordingCli()
    capabilities = cli.capabilities(timeout_secs=0.2)

    assert capabilities["ready"] is True
    assert len(cli.timeouts) == 2
    assert 0 < cli.timeouts[1] < cli.timeouts[0] <= 0.2
