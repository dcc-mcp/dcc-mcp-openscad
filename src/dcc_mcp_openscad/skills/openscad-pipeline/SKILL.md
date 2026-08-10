---
name: openscad-pipeline
description: >-
  Inspect, validate, export, and render OpenSCAD models through the bounded
  standalone CLI. Use for deterministic SCAD build pipelines; do not use it
  for arbitrary command-line flags or source-code generation.
license: MIT
compatibility: "Python 3.7+; OpenSCAD 2021.01+; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: openscad
    layer: domain
    version: "0.1.0"  # x-release-please-version
    tags: [openscad, cad, parametric-modeling, pipeline]
    search-hint: >-
      OpenSCAD status capabilities inspect SCAD modules dependencies validate
      compile export STL 3MF DXF SVG render PNG typed parameters
    tools: tools.yaml
---

# OpenSCAD Pipeline

Use these tools for deterministic, file-backed OpenSCAD workflows. Start with
`get_status`, inspect unfamiliar files before compiling them, then use
`validate_model` before an export intended for downstream production.

All source and output paths must stay under
`DCC_MCP_OPENSCAD_ALLOWED_ROOTS`. Parameter overrides are encoded from JSON
scalars and arrays; raw `-D` expressions and arbitrary CLI flags are not
accepted. Exports are staged to a sibling temporary file and moved into place
only after OpenSCAD succeeds.

Use `render_preview` for PNGs and `export_model` for geometry or 2D artifacts.
Both are asynchronous, bounded by `timeout_secs`, and refuse replacement unless
`overwrite=true` is explicit.
