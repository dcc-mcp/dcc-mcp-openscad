#!/usr/bin/env bash
# Install one pinned OpenSCAD build for the real-hardware CI job and export a
# verified executable path as OPENSCAD_TEST_EXECUTABLE.
#
# The AppImage is downloaded only when the local copy is missing or fails its
# pinned SHA256, so a warm actions/cache entry skips the download entirely. A
# candidate is accepted only after it has actually printed an OpenSCAD version
# matching the requested tier, which proves the whole invocation path works
# before the test suite is allowed to run.
#
# Required environment:
#   OPENSCAD_VERSION    expected version stamp, e.g. 2021.01 or 2026.09.29
#   OPENSCAD_ASSET      file name of the AppImage
#   OPENSCAD_SHA256     pinned SHA256 of that AppImage
#   OPENSCAD_ASSET_URL  full download URL
#   GITHUB_ENV          (set by Actions) receives the resolved values
set -euo pipefail

: "${OPENSCAD_VERSION:?OPENSCAD_VERSION must be set}"
: "${OPENSCAD_ASSET:?OPENSCAD_ASSET must be set}"
: "${OPENSCAD_SHA256:?OPENSCAD_SHA256 must be set}"
: "${OPENSCAD_ASSET_URL:?OPENSCAD_ASSET_URL must be set}"

workdir="${OPENSCAD_CACHE_DIR:-${RUNNER_TEMP:-/tmp}/openscad-${OPENSCAD_VERSION}}"
mkdir -p "${workdir}"
cd "${workdir}"

sha_matches() {
  [ -f "${OPENSCAD_ASSET}" ] || return 1
  [ "$(sha256sum "${OPENSCAD_ASSET}" | awk '{print $1}')" = "${OPENSCAD_SHA256}" ]
}

if sha_matches; then
  echo "Reusing cached ${OPENSCAD_ASSET}"
else
  echo "::group::Download ${OPENSCAD_ASSET}"
  rm -f "${OPENSCAD_ASSET}"
  curl -fsSL --retry 3 -o "${OPENSCAD_ASSET}" "${OPENSCAD_ASSET_URL}"
  echo "::endgroup::"
  if ! sha_matches; then
    echo "::error::SHA256 mismatch for ${OPENSCAD_ASSET}"
    echo "expected: ${OPENSCAD_SHA256}"
    echo "actual:   $(sha256sum "${OPENSCAD_ASSET}" | awk '{print $1}')"
    exit 1
  fi
fi
echo "Verified ${OPENSCAD_ASSET} (sha256 ${OPENSCAD_SHA256})"

root="${workdir}/squashfs-root"
if [ ! -d "${root}" ]; then
  echo "::group::Extract ${OPENSCAD_ASSET}"
  chmod +x "${OPENSCAD_ASSET}"
  "./${OPENSCAD_ASSET}" --appimage-extract >/dev/null
  echo "::endgroup::"
fi

mapfile -t candidates < <(
  find "${root}" \( -type f -o -type l \) -name 'openscad' -perm -u+x 2>/dev/null | sort
)
if [ "${#candidates[@]}" -eq 0 ]; then
  echo "::error::No openscad binary inside ${root}"
  find "${root}" -maxdepth 3 2>/dev/null | sort | head -50
  exit 1
fi

# Runs a candidate and prints the version it reports. Diagnostics go to stderr
# because stdout is captured by the caller's $(...).
probe() {
  local executable="$1"
  local output
  if ! output="$(timeout 120 "${executable}" --version 2>/tmp/openscad-probe.err)"; then
    echo "candidate --version exited non-zero: ${executable}" >&2
    tail -20 /tmp/openscad-probe.err >&2 || true
    return 1
  fi
  # Accept "OpenSCAD version 2021.01" and any prefix/suffix on that line.
  local version
  version="$(printf '%s' "${output}" | sed -n 's/.*OpenSCAD[[:space:]]*[Vv]ersion[[:space:]]*:*[[:space:]]*\([0-9][0-9.]*\).*/\1/p' | head -1)"
  if [ -z "${version}" ]; then
    echo "candidate reported no parseable version: ${executable} (got: ${output})" >&2
    return 1
  fi
  printf '%s' "${version}"
}

# Some AppImages only run correctly through AppRun, which sets up the bundled
# library paths. Prefer AppRun when it exists, then fall back to the raw binary
# and finally to a wrapper that exports the bundled libraries explicitly.
build_attempts() {
  attempts=()
  if [ -e "${root}/AppRun" ]; then
    attempts+=("${root}/AppRun")
  fi
  attempts+=("${candidates[@]}")
}

attempts=()
build_attempts

for candidate in "${attempts[@]}"; do
  echo "Probing ${candidate}"
  if ! reported="$(probe "${candidate}")"; then
    continue
  fi
  if [ "${reported}" != "${OPENSCAD_VERSION}" ]; then
    echo "candidate reported OpenSCAD ${reported}, expected ${OPENSCAD_VERSION}" >&2
    continue
  fi
  echo "Resolved OpenSCAD ${reported} via ${candidate}"
  {
    echo "OPENSCAD_TEST_EXECUTABLE=${candidate}"
    echo "OPENSCAD_REAL_VERSION=${reported}"
  } >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
  exit 0
done

# Last resort: run the binary through a wrapper that exports the bundled libs.
for candidate in "${candidates[@]}"; do
  libdirs="$(find "${root}" -type d -name 'lib' -o -type d -name 'lib64' 2>/dev/null | tr '\n' ':')"
  wrapper="${workdir}/openscad-wrapper.sh"
  cat > "${wrapper}" <<WRAPPER
#!/usr/bin/env bash
export LD_LIBRARY_PATH="${libdirs}\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
exec "${candidate}" "\$@"
WRAPPER
  chmod +x "${wrapper}"
  echo "Probing ${candidate} via LD_LIBRARY_PATH wrapper"
  if ! reported="$(probe "${wrapper}")"; then
    continue
  fi
  if [ "${reported}" != "${OPENSCAD_VERSION}" ]; then
    echo "wrapper reported OpenSCAD ${reported}, expected ${OPENSCAD_VERSION}" >&2
    continue
  fi
  echo "Resolved OpenSCAD ${reported} via ${candidate} (LD_LIBRARY_PATH wrapper)"
  {
    echo "OPENSCAD_TEST_EXECUTABLE=${wrapper}"
    echo "OPENSCAD_REAL_VERSION=${reported}"
  } >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
  exit 0
done

echo "::error::No candidate OpenSCAD could run and report ${OPENSCAD_VERSION}"
exit 1
