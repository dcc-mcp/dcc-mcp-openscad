"""Fail the real-hardware job when the OpenSCAD test did not actually execute.

`pytest -m openscad` exits 0 when its only test is skipped because
`OPENSCAD_TEST_EXECUTABLE` is unset, which would turn a missing host into a
green run. This guard reads the JUnit report and requires a real, non-skipped
pass.

It also re-checks the version the host actually reported against the matrix the
adapter ships, so a CI tier can never drift outside the support it claims.
"""

from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ElementTree

# `src` is on the pytest path but not on the default sys.path here.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))


def _as_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _check_matrix(version: str) -> None:
    from dcc_mcp_openscad.compat import classify_host, unsupported_reason

    verdict = classify_host(version)
    if verdict["status"] != "supported":
        sys.exit(
            "the real host reported OpenSCAD %s, which the shipped matrix does not "
            "support: %s" % (version, unsupported_reason(verdict))
        )
    print(
        "OpenSCAD %s is inside the shipped matrix (status=%s, evidence=%s)"
        % (version, verdict["status"], verdict["evidence_kind"])
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.exit("usage: verify-openscad-run.py <junit.xml>")
    if not os.path.isfile(argv[1]):
        sys.exit("no JUnit report at %s: pytest -m openscad never ran" % argv[1])
    root = ElementTree.parse(argv[1]).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")

    tests = errors = failures = skipped = 0
    for suite in suites:
        tests += _as_int(suite.get("tests"))
        errors += _as_int(suite.get("errors"))
        failures += _as_int(suite.get("failures"))
        skipped += _as_int(suite.get("skipped"))

    if tests == 0:
        sys.exit("no real OpenSCAD test was collected; the marker selected nothing")
    if skipped:
        sys.exit(
            "%s of %s real OpenSCAD test(s) were skipped; the host did not run"
            % (skipped, tests)
        )
    if errors or failures:
        sys.exit("%s error(s) and %s failure(s) in the real OpenSCAD run" % (errors, failures))

    print("real OpenSCAD run executed %s test(s) with 0 skipped" % tests)

    version = os.environ.get("OPENSCAD_REAL_VERSION", "")
    if not version:
        sys.exit("OPENSCAD_REAL_VERSION was not exported; the host version is unknown")
    _check_matrix(version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
