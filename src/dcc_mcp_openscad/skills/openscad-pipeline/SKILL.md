---
name: openscad-pipeline
description: >-
  Inspect a connected OpenSCAD session through the DCC-MCP standalone CLI boundary.
  This first slice is read-only and does not execute arbitrary source.
license: MIT
compatibility: "OpenSCAD; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: openscad
    layer: domain
    version: "0.1.0"
    tags: "openscad,mcp,dcc,automation"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# OpenSCAD

Experimental first slice. Live host validation, version matrices, catalog
onboarding, and mutation tools are separate follow-up gates.

