from dcc_mcp_core.skill import run_main

from dcc_mcp_openscad.skill_tools import bridge_main

main = bridge_main("render_preview", "OpenSCAD preview rendered.")

if __name__ == "__main__":
    run_main(main)
