from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ._process import ProcessCleanupError, run_bounded_command

_PARAMETER_NAME = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_MODULE = re.compile(r"\bmodule\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_FUNCTION = re.compile(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(")
_ASSIGNMENT = re.compile(r"(?m)^\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*=(?!=)")
_DEPENDENCY = re.compile(r"\b(include|use)\s*<([^>\r\n]+)>")
_VERSION = re.compile(r"OpenSCAD(?:\s+Version:|\s+version)?\s*:?\s*([^\r\n]+)", re.I)

_DEFAULT_OUTPUT_EXTENSIONS = {
    ".3mf",
    ".amf",
    ".ast",
    ".csg",
    ".dxf",
    ".echo",
    ".nef3",
    ".nefdbg",
    ".off",
    ".pdf",
    ".png",
    ".stl",
    ".svg",
    ".term",
}
_COLOR_SCHEMES = {
    "BeforeDawn",
    "Cornfield",
    "DeepOcean",
    "Metallic",
    "Monotone",
    "Nature",
    "Solarized",
    "Starnight",
    "Sunset",
    "Tomorrow",
    "Tomorrow Night",
}


class OpenScadError(RuntimeError):
    """A bounded, user-actionable OpenSCAD adapter failure."""


class OpenScadTimeoutError(OpenScadError):
    """OpenSCAD did not complete before the configured deadline."""


def _split_roots(value: str) -> list[Path]:
    roots = []
    for item in value.split(os.pathsep):
        item = item.strip()
        if item:
            roots.append(Path(item).expanduser().resolve())
    return roots


def _within(path: Path, roots: Sequence[Path]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        try:
            if os.path.commonpath((candidate, os.path.normcase(str(root)))) == os.path.normcase(
                str(root)
            ):
                return True
        except ValueError:
            continue
    return False


def _strip_comments_and_strings(source: str) -> str:
    """Remove comments/string contents while preserving line boundaries."""

    output = []
    index = 0
    state = "code"
    while index < len(source):
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if state == "code":
            if char == "/" and nxt == "/":
                output.extend("  ")
                index += 2
                state = "line-comment"
                continue
            if char == "/" and nxt == "*":
                output.extend("  ")
                index += 2
                state = "block-comment"
                continue
            if char == '"':
                output.append(" ")
                index += 1
                state = "string"
                continue
            output.append(char)
        elif state == "line-comment":
            if char in "\r\n":
                output.append(char)
                state = "code"
            else:
                output.append(" ")
        elif state == "block-comment":
            if char == "*" and nxt == "/":
                output.extend("  ")
                index += 2
                state = "code"
                continue
            output.append(char if char in "\r\n" else " ")
        else:
            if char == "\\" and nxt:
                output.extend("  ")
                index += 2
                continue
            if char == '"':
                output.append(" ")
                state = "code"
            else:
                output.append(char if char in "\r\n" else " ")
        index += 1
    return "".join(output)


def _scad_literal(value: Any, depth: int = 0) -> str:
    if depth > 4:
        raise OpenScadError("Parameter arrays may not be nested more than four levels")
    if value is None:
        return "undef"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not (float("-inf") < value < float("inf")):
            raise OpenScadError("Parameter numbers must be finite")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        if len(value) > 1_000:
            raise OpenScadError("Parameter arrays are limited to 1000 values")
        return "[" + ", ".join(_scad_literal(item, depth + 1) for item in value) + "]"
    raise OpenScadError("Parameters support only null, booleans, numbers, strings, and arrays")


def _parameter_args(parameters: Optional[Mapping[str, Any]]) -> list[str]:
    if not parameters:
        return []
    if len(parameters) > 64:
        raise OpenScadError("At most 64 OpenSCAD parameters may be supplied")
    result = []
    for name in sorted(parameters):
        if not _PARAMETER_NAME.fullmatch(name):
            raise OpenScadError("Invalid OpenSCAD parameter name: %s" % name)
        result.extend(("-D", "%s=%s" % (name, _scad_literal(parameters[name]))))
    return result


def _diagnostics(stdout: str, stderr: str) -> list[dict[str, str]]:
    diagnostics = []
    seen = set()
    for line in (stderr + "\n" + stdout).splitlines():
        message = line.strip()
        upper = message.upper()
        if upper.startswith("ERROR:"):
            severity = "error"
        elif upper.startswith(("WARNING:", "DEPRECATED:")):
            severity = "warning"
        else:
            continue
        key = (severity, message)
        if key not in seen:
            diagnostics.append({"severity": severity, "message": message})
            seen.add(key)
    return diagnostics


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class OpenscadCli:
    """Typed, workspace-bounded wrapper around the standalone OpenSCAD CLI."""

    def __init__(
        self,
        executable: Optional[str] = None,
        allowed_roots: Optional[Iterable[Path]] = None,
        max_source_bytes: int = 4 * 1024 * 1024,
        max_timeout_secs: float = 1_800,
    ):
        self.executable = self._resolve_executable(executable)
        roots = list(allowed_roots or (Path.cwd(),))
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.max_source_bytes = max(1, int(max_source_bytes))
        self.max_timeout_secs = max(1.0, float(max_timeout_secs))

    @classmethod
    def from_env(cls) -> "OpenscadCli":
        roots_value = os.environ.get("DCC_MCP_OPENSCAD_ALLOWED_ROOTS", "")
        roots = _split_roots(roots_value) if roots_value else [Path.cwd().resolve()]
        return cls(
            os.environ.get("DCC_MCP_OPENSCAD_EXECUTABLE") or None,
            allowed_roots=roots,
            max_source_bytes=int(
                os.environ.get("DCC_MCP_OPENSCAD_MAX_SOURCE_BYTES", str(4 * 1024 * 1024))
            ),
            max_timeout_secs=float(os.environ.get("DCC_MCP_OPENSCAD_MAX_TIMEOUT_SECS", "1800")),
        )

    @staticmethod
    def _resolve_executable(explicit: Optional[str]) -> Optional[str]:
        candidates = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        else:
            for name in ("openscad.com", "openscad"):
                found = shutil.which(name)
                if found:
                    candidates.append(Path(found))
            if os.name == "nt":
                candidates.extend(
                    (
                        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                        / "OpenSCAD"
                        / "openscad.com",
                        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                        / "OpenSCAD"
                        / "openscad.exe",
                    )
                )
        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_dir():
                candidate = candidate / ("openscad.com" if os.name == "nt" else "openscad")
            if os.name == "nt" and candidate.suffix.lower() == ".exe":
                console = candidate.with_suffix(".com")
                if console.is_file():
                    candidate = console
            if candidate.is_file():
                return str(candidate)
        return None

    def _source_path(self, value: str) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() != ".scad":
            raise OpenScadError("Only .scad source files are accepted")
        if not path.is_file():
            raise OpenScadError("SCAD source file does not exist: %s" % path)
        if not _within(path, self.allowed_roots):
            raise OpenScadError("Source file is outside DCC_MCP_OPENSCAD_ALLOWED_ROOTS")
        if path.stat().st_size > self.max_source_bytes:
            raise OpenScadError("SCAD source exceeds the configured size limit")
        return path

    def _output_path(self, value: str, required_suffix: Optional[str] = None) -> Path:
        path = Path(value).expanduser().resolve()
        if required_suffix and path.suffix.lower() != required_suffix:
            raise OpenScadError("Output path must end with %s" % required_suffix)
        if not path.parent.is_dir():
            raise OpenScadError("Output directory does not exist: %s" % path.parent)
        if not _within(path, self.allowed_roots):
            raise OpenScadError("Output file is outside DCC_MCP_OPENSCAD_ALLOWED_ROOTS")
        return path

    def _timeout(self, value: float) -> float:
        timeout = float(value)
        if not math.isfinite(timeout) or timeout <= 0 or timeout > self.max_timeout_secs:
            raise OpenScadError(
                "timeout_secs must be greater than 0 and no more than %s"
                % int(self.max_timeout_secs)
            )
        return timeout

    def _run(self, args: Sequence[str], timeout_secs: float, cwd: Optional[Path] = None) -> dict:
        if not self.executable:
            raise OpenScadError("OpenSCAD CLI was not found; set DCC_MCP_OPENSCAD_EXECUTABLE")
        timeout = self._timeout(timeout_secs)
        command = [self.executable] + [str(item) for item in args]
        started = time.monotonic()
        try:
            result = run_bounded_command(command, timeout=timeout, cwd=cwd)
        except ProcessCleanupError:
            raise OpenScadError(
                "OpenSCAD process supervision failed: probe_cleanup_failed"
            ) from None
        reason = result.get("reason")
        if reason == "probe_timeout":
            raise OpenScadTimeoutError("OpenSCAD exceeded the configured timeout")
        if reason in {
            "probe_cleanup_failed",
            "probe_launch_failed",
            "probe_status_invalid",
            "probe_supervisor_failed",
        }:
            raise OpenScadError("OpenSCAD process supervision failed: %s" % reason)
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        truncated = bool(result.get("truncated"))
        return {
            "returncode": int(result.get("returncode") or 0),
            "duration_secs": round(time.monotonic() - started, 3),
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": truncated,
            "stderr_truncated": truncated,
            "diagnostics": _diagnostics(stdout, stderr),
        }

    def status(self, timeout_secs: float = 20) -> dict[str, Any]:
        timeout = self._timeout(timeout_secs)
        return self._status_until(time.monotonic() + timeout, timeout)

    def _status_until(
        self, deadline: float, initial_budget: Optional[float] = None
    ) -> dict[str, Any]:
        if not self.executable:
            return {
                "ready": False,
                "executable": None,
                "instance_type": "standalone",
                "reason": "openscad_not_found",
                "allowed_roots": [str(root) for root in self.allowed_roots],
            }
        remaining = self._remaining(deadline)
        if initial_budget is not None:
            remaining = min(remaining, initial_budget)
        version_run = self._run(("--version",), min(10, remaining))
        version_output = (version_run["stdout"] + "\n" + version_run["stderr"]).strip()
        if not version_output:
            info_run = self._run(("--info",), self._remaining(deadline))
            version_output = (info_run["stdout"] + "\n" + info_run["stderr"]).strip()
            ready = info_run["returncode"] == 0
        else:
            ready = version_run["returncode"] == 0
        match = _VERSION.search(version_output)
        return {
            "ready": ready,
            "executable": self.executable,
            "instance_type": "standalone",
            "version": match.group(1).strip() if match else version_output.splitlines()[0],
            "allowed_roots": [str(root) for root in self.allowed_roots],
            "max_source_bytes": self.max_source_bytes,
            "max_timeout_secs": self.max_timeout_secs,
        }

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenScadTimeoutError("OpenSCAD exceeded the configured timeout")
        return remaining

    def capabilities(
        self,
        status: Optional[Mapping[str, Any]] = None,
        timeout_secs: float = 20,
    ) -> dict[str, Any]:
        timeout = self._timeout(timeout_secs)
        deadline = time.monotonic() + timeout
        status = dict(status) if status is not None else self._status_until(deadline, timeout)
        if not status["ready"]:
            return {"ready": False, "status": status, "flags": {}, "output_extensions": []}
        help_run = self._run(("--help",), min(10, self._remaining(deadline)))
        help_text = help_run["stdout"] + "\n" + help_run["stderr"]
        flags = {
            name: name in help_text
            for name in (
                "--animate",
                "--backend",
                "--check-parameter-ranges",
                "--check-parameters",
                "--hardwarnings",
                "--help-export",
                "--info",
                "--render",
                "--summary-file",
            )
        }
        return {
            "ready": help_run["returncode"] == 0,
            "reason": None if help_run["returncode"] == 0 else "openscad_help_failed",
            "status": status,
            "flags": flags,
            "output_extensions": sorted(_DEFAULT_OUTPUT_EXTENSIONS),
            "parameter_overrides": True,
            "customizer_parameter_sets": "-p" in help_text and "-P" in help_text,
            "png_color_schemes": sorted(_COLOR_SCHEMES),
        }

    def inspect_file(self, path: str) -> dict[str, Any]:
        source_path = self._source_path(path)
        source = source_path.read_text(encoding="utf-8-sig")
        sanitized = _strip_comments_and_strings(source)
        dependencies = []
        for kind, name in _DEPENDENCY.findall(sanitized):
            local_path = (source_path.parent / name).resolve()
            dependencies.append(
                {
                    "kind": kind,
                    "reference": name,
                    "local_path": str(local_path) if local_path.is_file() else None,
                    "local_exists": local_path.is_file(),
                }
            )
        return {
            "path": str(source_path),
            "bytes": source_path.stat().st_size,
            "lines": len(source.splitlines()),
            "modules": sorted(set(_MODULE.findall(sanitized))),
            "functions": sorted(set(_FUNCTION.findall(sanitized))),
            "assigned_variables": sorted(set(_ASSIGNMENT.findall(sanitized))),
            "dependencies": dependencies,
        }

    def validate_model(
        self,
        path: str,
        parameters: Optional[Mapping[str, Any]] = None,
        strict: bool = True,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        source_path = self._source_path(path)
        with tempfile.TemporaryDirectory(prefix="dcc-mcp-openscad-") as temp_dir:
            output = Path(temp_dir) / "validation.csg"
            deps = Path(temp_dir) / "validation.deps"
            args = []
            if strict:
                args.append("--hardwarnings")
            args.extend(("--check-parameters", "true", "--check-parameter-ranges", "true"))
            args.extend(_parameter_args(parameters))
            args.extend(("-d", str(deps), "-o", str(output), str(source_path)))
            result = self._run(args, timeout_secs, cwd=source_path.parent)
            errors = [item for item in result["diagnostics"] if item["severity"] == "error"]
            valid = result["returncode"] == 0 and output.is_file() and not errors
            result.update(
                {
                    "valid": valid,
                    "source_path": str(source_path),
                    "compiled_bytes": output.stat().st_size if output.is_file() else 0,
                    "strict": bool(strict),
                }
            )
            return result

    def _export(
        self,
        source_path: Path,
        output_path: Path,
        args: Sequence[str],
        timeout_secs: float,
        overwrite: bool,
    ) -> dict[str, Any]:
        replaced_existing = output_path.exists()
        if replaced_existing and not overwrite:
            raise OpenScadError("Output already exists; set overwrite=true to replace it")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output_path.stem,
            suffix=output_path.suffix,
            dir=str(output_path.parent),
        )
        os.close(descriptor)
        temp_output = Path(temp_name)
        temp_output.unlink()
        try:
            command = list(args) + ["-o", str(temp_output), str(source_path)]
            result = self._run(command, timeout_secs, cwd=source_path.parent)
            errors = [item for item in result["diagnostics"] if item["severity"] == "error"]
            if result["returncode"] != 0 or errors or not temp_output.is_file():
                raise OpenScadError(
                    "OpenSCAD export failed: %s"
                    % (errors[0]["message"] if errors else "no output was produced")
                )
            if temp_output.stat().st_size <= 0:
                raise OpenScadError("OpenSCAD produced an empty output file")
            os.replace(str(temp_output), str(output_path))
            digest = _sha256_file(output_path)
            result.update(
                {
                    "source_path": str(source_path),
                    "output_path": str(output_path),
                    "bytes": output_path.stat().st_size,
                    "sha256": digest,
                    "overwritten": replaced_existing,
                }
            )
            return result
        finally:
            if temp_output.exists():
                temp_output.unlink()

    def export_model(
        self,
        source_path: str,
        output_path: str,
        parameters: Optional[Mapping[str, Any]] = None,
        stl_encoding: str = "binary",
        strict: bool = True,
        overwrite: bool = False,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        source = self._source_path(source_path)
        output = self._output_path(output_path)
        suffix = output.suffix.lower()
        if suffix == ".png" or suffix not in _DEFAULT_OUTPUT_EXTENSIONS:
            raise OpenScadError(
                "Unsupported model output extension; use render_preview for PNG outputs"
            )
        args = []
        if strict:
            args.append("--hardwarnings")
        args.extend(("--check-parameters", "true", "--check-parameter-ranges", "true"))
        args.extend(_parameter_args(parameters))
        if suffix == ".stl":
            if stl_encoding not in ("ascii", "binary"):
                raise OpenScadError("stl_encoding must be ascii or binary")
            args.extend(("--export-format", "asciistl" if stl_encoding == "ascii" else "binstl"))
        return self._export(source, output, args, timeout_secs, overwrite)

    def render_preview(
        self,
        source_path: str,
        output_path: str,
        width: int = 1200,
        height: int = 800,
        full_render: bool = False,
        projection: str = "perspective",
        color_scheme: str = "Cornfield",
        camera: Optional[Sequence[float]] = None,
        parameters: Optional[Mapping[str, Any]] = None,
        overwrite: bool = False,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        source = self._source_path(source_path)
        output = self._output_path(output_path, ".png")
        if not 64 <= int(width) <= 8_192 or not 64 <= int(height) <= 8_192:
            raise OpenScadError("PNG width and height must be between 64 and 8192")
        if projection not in ("orthographic", "perspective"):
            raise OpenScadError("projection must be orthographic or perspective")
        if color_scheme not in _COLOR_SCHEMES:
            raise OpenScadError("Unsupported OpenSCAD color scheme")
        args = [
            "--imgsize",
            "%d,%d" % (int(width), int(height)),
            "--projection",
            "o" if projection == "orthographic" else "p",
            "--colorscheme",
            color_scheme,
            "--autocenter",
            "--viewall",
        ]
        if full_render:
            args.append("--render")
        if camera is not None:
            if len(camera) not in (6, 7) or any(
                not isinstance(item, (int, float)) for item in camera
            ):
                raise OpenScadError("camera must contain six or seven finite numbers")
            if any(not (float("-inf") < float(item) < float("inf")) for item in camera):
                raise OpenScadError("camera values must be finite")
            args.extend(("--camera", ",".join(str(float(item)) for item in camera)))
        args.extend(_parameter_args(parameters))
        result = self._export(source, output, args, timeout_secs, overwrite)
        result.update(
            {
                "width": int(width),
                "height": int(height),
                "full_render": bool(full_render),
                "projection": projection,
                "color_scheme": color_scheme,
            }
        )
        return result


def get_bridge() -> OpenscadCli:
    return OpenscadCli.from_env()
