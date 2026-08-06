# dcc-mcp-openscad

OpenSCAD adapter foundation for the DCC-MCP organization.

This is an experimental, read-only first slice. It is **not** in the released
`dcc-mcp-cli dcc-types` catalog yet.

## Scope

- Discover the standalone CLI boundary.
- Expose one typed, read-only SCAD file inspection tool.
- Keep host API calls outside the MCP HTTP worker.
- Do not expose arbitrary source evaluation.

## Install

```bash
python -m pip install -e ".[test]"
dcc-mcp-openscad
```

Configure the bridge environment variables in `src/dcc_mcp_openscad/bridge.py`.
A real OpenSCAD live smoke is required before catalog onboarding.

Official API reference: https://files.openscad.org/documentation/manual/OpenSCAD_User_Manual.pdf

