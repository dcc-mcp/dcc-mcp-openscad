from dcc_mcp_core.skill import run_main, skill_entry, skill_success

from dcc_mcp_openscad.bridge import get_bridge


@skill_entry
def main(path: str, **_kwargs):
    return skill_success("OpenSCAD file inspected.", **get_bridge().inspect_file(path))


if __name__ == "__main__":
    run_main(main)
