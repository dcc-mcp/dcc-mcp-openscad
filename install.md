# OpenSCAD Standalone Install SOP v1

This adapter invokes an OS-managed OpenSCAD command-line executable. Nothing
is installed into the OpenSCAD application. The canonical raw guide is
<https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-openscad/main/install.md>.

## Requirements

- OpenSCAD 2021.01 or newer from the official project or an operating-system
  package repository.
- External Python 3.7 or newer with `dcc-mcp-core>=0.20.14,<1.0.0`.
- Read/write access to each path in `DCC_MCP_OPENSCAD_ALLOWED_ROOTS`.

The adapter does not download, scrape, update, or execute a remote OpenSCAD
payload. There is no adapter-managed binary cache. OpenSCAD remains owned by
the operating-system package manager or official installation.

## Supported versions

| Platform | Automatic discovery | Explicit example |
| --- | --- | --- |
| Windows | `openscad.com` then `openscad` on `PATH`, followed by `%ProgramFiles%\OpenSCAD` | `C:\Program Files\OpenSCAD\openscad.com` |
| macOS | `openscad` on `PATH`; otherwise use the application bundle executable | `/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD` |
| Linux | `openscad` on `PATH`, normally provided by the distribution package manager | `/usr/bin/openscad` |

On Windows, use `openscad.com` for machine-readable console behavior. If an
`openscad.exe` override has a sibling `openscad.com`, the adapter selects the
`.com` launcher automatically. The minimum supported version is 2021.01.

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
| `10` | Discovery, configuration, Core floor, or 2021.01 host floor failed |
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
- `host_version`, exit `10`: select OpenSCAD 2021.01 or newer.
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
