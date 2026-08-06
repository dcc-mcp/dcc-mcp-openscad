from pathlib import Path

from dcc_mcp_openscad.bridge import OpenscadCli


def test_inspect_file(tmp_path: Path):
    p = tmp_path / "sample.scad"
    p.write_text("module cube_part() {}\ninclude <shared.scad>\n", encoding="utf-8")
    result = OpenscadCli().inspect_file(str(p))
    assert result["lines"] == 2 and result["has_module"] and result["has_include"]
