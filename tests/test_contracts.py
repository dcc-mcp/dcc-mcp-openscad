from pathlib import Path

import yaml
from dcc_mcp_core import validate_skill

from dcc_mcp_openscad.server import OpenscadMcpServer

ROOT = Path(__file__).parents[1]
SKILL = ROOT / "src" / "dcc_mcp_openscad" / "skills" / "openscad-pipeline"


def test_skill_contract_is_valid():
    report = validate_skill(str(SKILL))
    errors = [issue.message for issue in report.issues if issue.severity == "error"]
    assert errors == []


def test_all_tools_are_typed_bounded_and_affinity_explicit():
    payload = yaml.safe_load((SKILL / "tools.yaml").read_text(encoding="utf-8"))
    tools = payload["tools"]

    assert {tool["name"] for tool in tools} == {
        "get_status",
        "get_capabilities",
        "inspect_source",
        "validate_model",
        "export_model",
        "render_preview",
    }
    for tool in tools:
        assert tool["input_schema"]["type"] == "object"
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["output_schema"]["type"] == "object"
        assert tool["affinity"] == "any"
        assert tool["enforce_thread_affinity"] is True
        assert "timeout_hint_secs" in tool
        assert set(tool["annotations"]) == {
            "read_only_hint",
            "destructive_hint",
            "idempotent_hint",
            "open_world_hint",
            "deferred_hint",
        }


def test_server_declares_standalone_lifetime():
    server = OpenscadMcpServer(port=0)
    options = next(value for value in vars(server).values() if hasattr(value, "instance_type"))
    assert options.instance_type == "standalone"
