from dcc_mcp_core.skill import run_main, skill_entry, skill_success

from dcc_mcp_openscad.source_authoring import write_model_source


@skill_entry
def main(output_path: str, source: str):
    result = write_model_source(output_path, source)
    checks = result.pop("verified")
    return skill_success(
        "New self-contained OpenSCAD source written and verified.",
        verified=True,
        verification_checks=checks,
        **result,
    )


if __name__ == "__main__":
    run_main(main)
