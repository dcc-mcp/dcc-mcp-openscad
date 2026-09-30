"""Assert that a real, out-of-matrix OpenSCAD is refused on a real binary.

The `openscad-real` job proves a supported host works. This is its mirror: it
proves the matrix is actually enforced, rather than merely declared. A host
below the covered ranges must be rejected with EXIT_PREFLIGHT and the
`openscad_host_version_unsupported` error code -- not run unverified.

The 2019.05 AppImage is used because, like 2021.01, it is a permanent release
URL: a leg pinned to it cannot rot the way a dated snapshot can.
"""

from __future__ import annotations

import json
import subprocess
import sys


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: assert-unsupported-host.py <openscad-executable>")
    executable = argv[1]

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "dcc_mcp_openscad.server",
            "doctor",
            "--executable",
            executable,
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        report = json.loads(completed.stdout)
    except ValueError:
        sys.exit(
            "doctor did not emit JSON for %s (exit %s)\nstdout: %s\nstderr: %s"
            % (executable, completed.returncode, completed.stdout[:2000], completed.stderr[:2000])
        )

    version = (report.get("runtime") or {}).get("version")
    matrix = (report.get("runtime") or {}).get("host_matrix") or {}
    print(
        "doctor on %s: exit=%s version=%s status=%s error_code=%s"
        % (
            executable,
            completed.returncode,
            version,
            matrix.get("status"),
            report.get("error_code"),
        )
    )

    if completed.returncode != 10:
        sys.exit(
            "expected exit 10 (preflight) for an out-of-matrix host, got %s" % completed.returncode
        )
    if report.get("status") != "failed":
        sys.exit("expected status 'failed', got %r" % report.get("status"))
    if report.get("error_code") != "openscad_host_version_unsupported":
        sys.exit(
            "expected error_code 'openscad_host_version_unsupported', got %r"
            % report.get("error_code")
        )
    if matrix.get("status") != "too_old":
        sys.exit("expected matrix status 'too_old', got %r" % matrix.get("status"))
    if report.get("verify", {}).get("directly_usable") is not False:
        sys.exit("an out-of-matrix host must not be reported as directly usable")

    print(
        "OpenSCAD %s was refused as expected (%s)"
        % (version, ", ".join(matrix.get("supported_ranges") or ()) or "no ranges")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
