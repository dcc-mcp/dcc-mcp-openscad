# dcc-mcp-openscad

<p align="center">
  <img src="docs/assets/dcc-mcp-openscad.svg" alt="DCC-MCP · OPENSCAD" width="600">
</p>

Production OpenSCAD adapter for deterministic model inspection, validation,
geometry export, and PNG rendering through DCC-MCP.

## Capabilities

- Detect the standalone OpenSCAD CLI and report version-specific capabilities.
- Inspect modules, functions, variables, and `include`/`use` references without
  executing source.
- Validate a parameterized model with structured warning/error diagnostics.
- Export STL (explicit binary or ASCII), 3MF, AMF, OFF, CSG, DXF, SVG, PDF,
  and OpenSCAD diagnostic formats.
- Render bounded PNG previews or full geometry renders with typed camera,
  projection, color-scheme, size, and parameter inputs.
- Stage outputs atomically and return file size plus SHA-256 provenance.

The adapter does not accept arbitrary OpenSCAD command-line arguments or raw
`-D` expressions. JSON scalar/array parameters are encoded by the adapter.

## Requirements

- Python 3.7+
- `dcc-mcp-core` 0.19.91+
- OpenSCAD 2021.01 or newer

On Windows, point to `openscad.com` when possible; if `openscad.exe` is
configured and a sibling `openscad.com` exists, the adapter selects the console
launcher automatically.

## Install

```bash
python -m pip install dcc-mcp-openscad
dcc-mcp-openscad
```

For development:

```bash
python -m pip install -e ".[dev]"
python -m pytest
```

## Configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `DCC_MCP_OPENSCAD_EXECUTABLE` | Exact OpenSCAD executable or install directory | `PATH` and standard install locations |
| `DCC_MCP_OPENSCAD_ALLOWED_ROOTS` | `os.pathsep`-separated source/output roots | server working directory |
| `DCC_MCP_OPENSCAD_MAX_SOURCE_BYTES` | Maximum source size | 4 MiB |
| `DCC_MCP_OPENSCAD_MAX_TIMEOUT_SECS` | Maximum per-call deadline | 1800 seconds |
| `DCC_MCP_OPENSCAD_PORT` | Fixed adapter port when direct addressing is required | OS-assigned |

The service registers as a standalone DCC-MCP instance. It has no bound GUI
PID and needs no OpenSCAD window.

## Agent workflow

Use the standard discovery path:

```bash
dcc-mcp-cli list
dcc-mcp-cli search --query "OpenSCAD validate export STL"
dcc-mcp-cli load-skill openscad-pipeline --dcc-type openscad --instance-id <instance-short>
dcc-mcp-cli describe openscad.<instance-short>.openscad_pipeline__validate_model
```

Then call `inspect_source` → `validate_model` → `export_model` or
`render_preview`. Long calls are asynchronous and should be polled through the
core job tools returned by the call.

`examples/production_smoke.scad` is a parameterized enclosure used by the live
acceptance test. Override `width`, `depth`, `height`, `wall`, `corner_radius`,
or `vent_count` to verify safe parameter encoding and reproducible artifacts.

## Safety contract

- Source and output paths must remain under configured allowed roots.
- Existing outputs are never replaced unless `overwrite=true` is explicit.
- Exports use sibling temporary files and become visible only after success.
- Parameter names, values, nesting, count, image dimensions, formats, and
  deadlines are bounded.
- Cancellation and timeouts terminate the owned OpenSCAD child process.
- Captured process output is limited to 64 KiB per stream.

OpenSCAD CLI reference:
<https://en.wikibooks.org/wiki/OpenSCAD_User_Manual/Using_OpenSCAD_in_a_command_line_environment>
