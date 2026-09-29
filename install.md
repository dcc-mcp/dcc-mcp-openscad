# OpenSCAD Standalone Install SOP v1

This adapter invokes an OS-managed OpenSCAD command-line executable. Nothing
is installed into the OpenSCAD application. The canonical raw guide is
<https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-openscad/main/install.md>.

## Requirements

- OpenSCAD 2021.01 or newer from the official project or an operating-system
  package repository, and inside the supported ranges below. "Newer" alone is
  not sufficient: a build above the matrix (for example `2027.01`) is rejected
  rather than assumed compatible.
- External Python 3.7 or newer with `dcc-mcp-core>=0.20.36,<1.0.0`.
- Read/write access to each path in `DCC_MCP_OPENSCAD_ALLOWED_ROOTS`.

The adapter does not download, scrape, update, or execute a remote OpenSCAD
payload. There is no adapter-managed binary cache. OpenSCAD remains owned by
the operating-system package manager or official installation.

## Runtime model: two separate environments

This adapter runs in two environments that do **not** share a version, an
interpreter, or an upgrade path. Keeping them apart in your head prevents most
installation surprises:

| | Host side (the adapter) | OpenSCAD side (the tool) |
| --- | --- | --- |
| What it is | A Python package you install with `pip` | A standalone executable on the machine |
| Language | Python, `requires-python >= 3.7` | Not Python: a compiled C++ program |
| Version source | `dcc-mcp-openscad` and `dcc-mcp-core` wheel versions | The OpenSCAD build you installed (for example `2021.01`) |
| How it is upgraded | `pip install --upgrade dcc-mcp-openscad` | Your OS package manager or the official OpenSCAD installer |
| How the adapter finds it | n/a — this is the code that runs | `DCC_MCP_OPENSCAD_EXECUTABLE`, `PATH`, or a standard install location |

Consequences worth stating explicitly:

- **The Python version and the OpenSCAD version are independent.** Installing
  Python 3.12 does not change which OpenSCAD is used, and upgrading OpenSCAD
  does not affect the adapter's Python requirements. The `requires-python`
  floor applies only to the host side; it says nothing about OpenSCAD, which
  is not a Python program at all.
- **The two sides only meet at the CLI boundary.** The adapter starts the
  OpenSCAD executable as a child process, passes typed flags, and reads the
  artifacts it writes. There is no embedded interpreter, plugin, or shared
  library in either direction.
- **Each side is verified separately.** `dcc-mcp-openscad doctor --json`
  reports the Python/Core side and then probes the OpenSCAD executable,
  reporting the measured version, the capability flags it found, and whether
  that version is inside the supported matrix.

## Supported versions

Host support is decided by a machine-readable matrix shipped inside the wheel
(`src/dcc_mcp_openscad/compat_matrix.json`), not by a single minimum. A version
outside the declared ranges is rejected with an explicit error naming the
measured version and the covered ranges; it is never silently downgraded.

| Range | Status | Evidence grade | CI build that proves it |
| --- | --- | --- | --- |
| `2021.01` | supported | `real_ci` — a pinned build runs the adapter end-to-end in CI | `OpenSCAD-2021.01-x86_64.AppImage` |
| `2021.02`–`2026.08` | supported | `static` — CLI surface reviewed; **no CI run for these builds** | none |
| `2026.09` | supported | `real_ci` — a pinned build runs the adapter end-to-end in CI | `OpenSCAD-2026.09.29-x86_64.AppImage` |

The two grades are not equivalent, and the matrix records which is which:
`real_ci` means the exact build above executed status, validate, export and
render in CI with zero skipped tests; `static` means every flag the adapter
emits is present in the help output of both endpoints and no removal is
declared for the span, but no pinned build in the span is executed. Treat the
`static` range as lower confidence.

| Platform | Automatic discovery | Explicit example |
| --- | --- | --- |
| Windows | `openscad.com` then `openscad` on `PATH`, followed by `%ProgramFiles%\OpenSCAD` | `C:\Program Files\OpenSCAD\openscad.com` |
| macOS | `openscad` on `PATH`; otherwise use the application bundle executable | `/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD` |
| Linux | `openscad` on `PATH`, normally provided by the distribution package manager | `/usr/bin/openscad` |

On Windows, use `openscad.com` for machine-readable console behavior. If an
`openscad.exe` override has a sibling `openscad.com`, the adapter selects the
`.com` launcher automatically. The declared floor is 2021.01.

## Agent quick path

Install the released Python wheel, then run the bounded version and capability
probe before starting the service:

```shell
python -m pip install dcc-mcp-openscad
dcc-mcp-openscad doctor --json
dcc-mcp-openscad install --dry-run --json
dcc-mcp-openscad install --yes --json
```

Proceed only when the JSON response has exit `0` and
`verify.directly_usable=true`. Follow the structured `next_steps` command for
other outcomes. `install` records the exact external Python, Core module, and
OS-managed OpenSCAD executable identities; it never installs or downloads the
OpenSCAD application.

Set the allowed workspace before starting the foreground adapter service:

```shell
# Windows PowerShell example
$env:DCC_MCP_OPENSCAD_ALLOWED_ROOTS = "D:\models;D:\exports"
dcc-mcp-openscad
```

With no verb, `dcc-mcp-openscad` retains the existing service-start behavior.

## Manual path

When discovery is ambiguous, provide the absolute OS-managed executable:

```shell
dcc-mcp-openscad doctor --executable <absolute-openscad-cli> --json
```

The persistent environment equivalent is:

```text
DCC_MCP_OPENSCAD_EXECUTABLE=<absolute-openscad-cli>
DCC_MCP_OPENSCAD_RECEIPT=<absolute-adapter-receipt-path>
DCC_MCP_OPENSCAD_ALLOWED_ROOTS=<path-list-using-the-platform-separator>
DCC_MCP_OPENSCAD_MAX_SOURCE_BYTES=4194304
DCC_MCP_OPENSCAD_MAX_TIMEOUT_SECS=1800
```

Windows path lists use `;`; macOS and Linux use `:`. Invalid numeric settings
or missing roots fail preflight. This standalone adapter has no plugin copy,
host-side Python installation, application registration, or binary cache. Its
receipt is adapter-owned metadata only and defaults to
`~/.dcc-mcp/receipts/openscad.json`.

## Lifecycle

The public CLI implements the official Core Install SOP schema with six verbs:

| Verb | Effect |
| --- | --- |
| `doctor` | Probe prerequisites and report a plan without mutation |
| `install` | Atomically record the selected, verified runtime identities |
| `status` | Compare the current runtime with the owned receipt |
| `verify` | Re-probe and freshly recapture every identity |
| `upgrade` | Replace an existing owned receipt after re-verification |
| `uninstall` | Remove only the owned adapter receipt |

Mutations require `--yes`; `--dry-run` returns the exact plan. A same-directory
lock serializes receipt changes. Installs and upgrades stage a complete receipt
and atomically replace it; a failed commit keeps the prior valid receipt.
Uninstall rolls a displaced receipt back if deletion cannot complete.

## Verify

Run an explicit deep check when selecting a particular installation:

```shell
dcc-mcp-openscad verify --executable <absolute-openscad-cli> --timeout-secs 20 --json
```

The result reports executable source and launcher, local CLI endpoint,
workspace/limit configuration, Core and OpenSCAD versions, detected flags, and
supported output extensions. Schema `1` includes
`verify.directly_usable`, `failure_stage`, `failure_reason`, and
machine-executable `next_steps`.

Stable exits are:

| Code | Meaning |
| --- | --- |
| `0` | Core, executable, OpenSCAD version, and capability probe are usable |
| `10` | Discovery, configuration, Core floor, or the host version matrix failed |
| `20` | External artifact acquisition failed (not used by this non-provisioning adapter) |
| `30` | Receipt installation, upgrade, lock, or rollback failed |
| `40` | The executable was found but runtime/capability verification failed |
| `50` | A host restart is required (not used by the standalone CLI) |

The repository's real CLI acceptance uses
`examples/production_smoke.scad` to validate, export STL, and render PNG through
the bounded adapter bridge. Run it only with an explicitly installed official
CLI:

```shell
OPENSCAD_TEST_EXECUTABLE=<absolute-openscad-cli> python -m pytest tests/test_bridge.py::test_real_openscad_validate_export_and_render -m openscad -q
```

The default CI matrix does not download an unpinned binary and therefore does
not claim this live CLI E2E. It does enforce doctor JSON and exit contracts on
all supported runner platforms.

## Upgrade

Stop the foreground adapter, upgrade the wheel or OpenSCAD through their own
trusted installers, inspect the lifecycle plan, then atomically replace the
owned receipt:

```shell
python -m pip install --upgrade dcc-mcp-openscad
dcc-mcp-openscad upgrade --dry-run --json
dcc-mcp-openscad upgrade --yes --json
```

No mutable "latest" binary URL is used. Python wheel caching is owned by pip;
inspect it with `python -m pip cache info` and deliberately clean it with
`python -m pip cache purge`. OpenSCAD package cache cleanup remains owned by the
selected OS package manager. The adapter itself has no cache to migrate.

## Uninstall

Stop the foreground service, remove the adapter-owned receipt, then remove the
Python wheel:

```shell
dcc-mcp-openscad uninstall --yes --json
python -m pip uninstall dcc-mcp-openscad
```

Remove OpenSCAD separately through the same trusted installer/package manager
only when the application itself is no longer required. The lifecycle command
does not remove the application, an operator-owned file, or a binary cache.

## Troubleshooting

- `executable_discovery`, exit `10`: put the CLI on `PATH`, set
  `DCC_MCP_OPENSCAD_EXECUTABLE`, or pass absolute `--executable`.
- On Windows, configure `openscad.com`; `openscad.exe` can attach GUI behavior
  and is automatically replaced by a sibling `.com` when available.
- `configuration`, exit `10`: fix numeric limits and ensure every allowed root
  exists and uses the correct platform path separator.
- `core_version`, exit `10`: upgrade Core in the external Python that owns the
  adapter wheel.
- `host_version`, exit `10`: the measured OpenSCAD version is outside the
  supported matrix. The failure reports the measured version, the covered
  ranges, and which range it fell into (`too_old`, `too_new`, `unlisted`, or
  `unknown`). Select a build inside a declared range rather than assuming a
  newer build is compatible.
- `receipt` or `ownership`, exit `30`/`40`: keep the receipt on a regular,
  non-reparse path and use `upgrade --yes` to adopt an intentionally changed
  Core, Python, or OpenSCAD executable.
- `lock` or `cleanup`, exit `30`/`40`: wait for the current lifecycle mutation
  to finish; cleanup failures are reported and never hidden as success.
- `runtime_start` or `runtime_timeout`, exit `40`: run `--version` directly as
  the same user, check executable permissions and endpoint path, then retry.
- `capability_probe`, exit `40`: verify `--help` succeeds for the same CLI;
  partial or GUI-only installations are not directly usable.
- A healthy Python service alone is not proof of a usable CLI. Require exit `0`
  and `verify.directly_usable=true` before export or render calls.
