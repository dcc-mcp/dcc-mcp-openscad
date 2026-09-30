# Post-write read-back contract

A mutating tool returns only after it has re-read the artifact from disk and
proven that this call's change is actually there.

Anything less is a bug.

## Why this exists here

`openscad` is a separate CLI process supervised by the adapter, and **it exits 0
on several paths that write nothing at all**: an output suffix it does not
recognise, a degenerate geometry, a swallowed export. An agent that receives an
affirmative answer keeps building on it and only discovers the problem steps
later, as an unrelated symptom -- the most expensive class of bug in an agent
loop.

So a mutating tool must never:

- report success because the CLI exited 0, without looking at the artifact;
- return a summary computed from the inputs instead of from the file;
- report success when the write produced an empty or truncated geometry.

## Which tools owe a read-back

The classification lives in `dcc_mcp_openscad/write_contract.py`:

| Tool | Class | Why |
| --- | --- | --- |
| `export_model` | mutating | writes a geometry or 2D artifact into the workspace |
| `render_preview` | mutating | writes a PNG into the workspace |
| `get_status` | read-only | observes the CLI |
| `get_capabilities` | read-only | observes the CLI |
| `inspect_source` | read-only | parses source, writes nothing |
| `validate_model` | read-only | compiles into a private temporary directory that is removed with the call |

A test fails if a tool is missing from both lists, so adding one forces the
author to decide whether it owes a read-back.

## What is checked

### Identity

The file at the final path must exist and be non-empty, and its size and
SHA-256 must match **the values the export recorded when it staged the
artifact**. This catches the "exited 0, wrote nothing" and "wrote somewhere
else" classes.

The comparison is against a recorded value, never against a second
measurement of the same file: a read-back that re-measures what it is checking
compares the file with itself and cannot fail. `verify_artifact` therefore
requires `sha256` and `bytes` in `expected` and fails closed
(`artifact.recorded_identity`) when they are missing, so the check can never
silently degrade into an existence test.

### Content

Identity alone would accept a well-formed file that carries no model, so the
artifact is parsed back far enough to show it holds what was asked for:

| Format | Check ids | What it proves |
| --- | --- | --- |
| binary STL | `stl.facets`, `stl.size_consistent` | the declared facet count is at least 1 and the byte length equals `84 + 50 × count` |
| ASCII STL | `stl.ascii_facets` | at least one `facet normal` block is present |
| PNG | `png.header`, `png.dimensions` | the IHDR is valid and the frame matches the requested width and height |
| other formats | identity checks only | the artifact was written; the adapter makes no claim about parsing it |

Every check that ran is listed in the result's `verified`, so a caller can see
which guards a format actually got instead of inferring it from the ones that
fired.

## What a failure carries

A mismatch raises `WriteVerificationError` with a structured payload, forwarded
verbatim across the process boundary so the caller can branch on it instead of
parsing prose:

```json
{
  "schema_version": 1,
  "tool": "export_model",
  "check": "stl.facets",
  "expected": "at least 1 facet",
  "actual": 0,
  "host_version": "2026.09.29",
  "host_matrix": {"status": "supported", "range": {"id": "2026.09.x"}},
  "params": {"source_path": "...", "output_path": "..."},
  "remediation": "OpenSCAD wrote an STL with no geometry; ..."
}
```

Both sides of the comparison are always present, and the host version always
travels with the mismatch: a read-back that disagrees is the classic signature
of host CLI drift, and without the version the report is unreproducible.

At the adapter boundary the error surfaces as
`OpenScadWriteVerificationError`, a subclass of `OpenScadError`, so callers
that already handle adapter failures keep working.
