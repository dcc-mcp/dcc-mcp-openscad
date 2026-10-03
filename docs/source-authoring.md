# Bounded native source authoring

`write_model_source(output_path, source)` writes a **new** `.scad` file beneath a configured `DCC_MCP_OPENSCAD_ALLOWED_ROOTS` workspace. Supply an absolute output path whose parent directory already exists.

The tool accepts at most 262144 UTF-8 bytes and refuses existing files, symlinks, traversal, NUL, and external `include`, `use`, `import`, or `surface` references. Windows paths must use ordinary filename components: alternate streams, reserved devices and trailing dots or spaces are rejected. The reference check is deliberately conservative: these words are rejected even inside comments. This is a controlled-workspace authoring operation, not a sandbox against concurrent directory replacement or hostile native programs.

Success is reported only after reading the new file back and comparing its bytes with the supplied UTF-8 source. The result includes byte count, SHA-256, and named verification checks. Existing files are never replaced. The tool does not compile or execute the submitted source; use `inspect_source`, `validate_model`, and `export_model` as separate operations under their existing bounded host contracts.

Named export checks are preserved under `context.verification_checks`, while `postcondition.verified` remains a boolean. This avoids passing the adapter's check list into Core's boolean result-envelope argument and incorrectly reporting a successful native export as an MCP error.

The source-qualification snapshot uses OpenSCAD 2021.01, Python 3.13, and official Core/server/CLI 0.20.41. Genuine MCP calls write and inspect a small synthetic model, validate it, export OFF and binary STL, verify a parameter change from 3×5×7 to 4×5×7, and reject bounded negative cases without changing the original source. Direct native-CLI test invocations are reported separately from MCP evidence. The headless PNG-preview test remains unqualified in that snapshot; the supported host/platform CI matrix must pass on the final PR head. Publication review adds Windows filename guards and regressions after that snapshot. See [publication validation](validation/source-authoring.md).

For isolated programmatic sessions, `OpenscadMcpServer` accepts explicit `gateway_port` and `enable_gateway_failover` keyword arguments. Omitting them preserves the previous environment/default behavior; qualification uses `gateway_port=0` and `enable_gateway_failover=False` without changing host or network settings.
