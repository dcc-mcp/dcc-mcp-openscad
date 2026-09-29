from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_install_guide_documents_standalone_wheel_and_verify_contract() -> None:
    guide = (ROOT / "install.md").read_text(encoding="utf-8")

    headings = [
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Lifecycle",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ]
    assert all(heading in guide for heading in headings)
    assert all(platform in guide for platform in ("Windows", "macOS", "Linux"))
    assert "python -m pip install dcc-mcp-openscad" in guide
    assert "pip install -e" not in guide
    assert "dcc-mcp-openscad doctor --json" in guide
    assert "dcc-mcp-openscad install --yes --json" in guide
    assert "dcc-mcp-openscad verify" in guide
    assert "dcc-mcp-openscad uninstall --yes --json" in guide
    assert all("`%s`" % code in guide for code in (0, 10, 20, 30, 40, 50))
    assert "dcc-mcp-core>=0.20.36,<1.0.0" in guide
    # The boundary is a supervised CLI child process, not an embedded interpreter.
    assert "Runtime boundary" in guide
    assert "host side" in guide and "OpenSCAD side" in guide
    assert "openscad_host_version_unlisted" in guide
    assert "compat_matrix.json" in guide
    assert "openscad.com" in guide
    assert "openscad.exe" in guide
    assert "does not download" in guide
    assert "no adapter-managed binary cache" in guide
    assert "examples/production_smoke.scad" in guide
    assert "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-openscad/main/install.md" in guide


def test_ci_runs_explicit_doctor_json_smoke() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    names = {step.get("name") for step in workflow["jobs"]["test"]["steps"]}

    assert "Doctor JSON smoke" in names


def test_ci_runs_the_real_openscad_case_and_fails_when_it_skips() -> None:
    """The real-host leg is the whole point: skipped must mean failed."""
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["openscad-real"]
    matrix = job["strategy"]["matrix"]["include"]

    # At least two host tiers, each pinned to an exact asset.
    assert len(matrix) >= 2
    for entry in matrix:
        assert entry["openscad_version"]
        assert entry["openscad_asset"]
        assert entry["openscad_asset"].endswith(".AppImage")

    commands = " ".join(str(step.get("run") or "") for step in job["steps"])
    assert "install-openscad.sh" in commands
    assert "verify-openscad-run.py" in commands
    assert "-m openscad" in commands
    # A skipped case must not be able to turn the leg green: the guard that
    # fails on a skip has to run even when pytest failed.
    guard = [step for step in job["steps"] if "verify-openscad-run.py" in (step.get("run") or "")]
    assert guard and guard[0].get("if") == "always()"
    # Rendering needs a display, so the real run is wrapped rather than dropped.
    assert "xvfb-run" in commands


def test_ci_also_proves_an_unsupported_host_is_refused_on_a_real_binary() -> None:
    """A supported host working means nothing if an old one is tolerated."""
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["openscad-old-host"]
    commands = " ".join(str(step.get("run") or "") for step in job["steps"])

    assert "install-openscad.sh" in commands
    assert "assert-unsupported-host.py" in commands
    # Pinned to a permanent release URL, not a dated snapshot that can rotate.
    pinned = " ".join(
        str(value) for step in job["steps"] for value in (step.get("env") or {}).values()
    )
    assert "OpenSCAD-2019.05-x86_64.AppImage" in pinned
    assert "https://files.openscad.org/snapshots" not in pinned


def test_install_guide_documents_the_supported_matrix() -> None:
    guide = (ROOT / "install.md").read_text(encoding="utf-8")

    from dcc_mcp_openscad.compat import supported_range_labels

    for label in supported_range_labels():
        assert label in guide
