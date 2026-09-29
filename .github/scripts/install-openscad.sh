#!/usr/bin/env bash
# Install one pinned OpenSCAD for the real-hardware CI job and export a
# verified executable path as OPENSCAD_TEST_EXECUTABLE.
#
# The AppImage is only downloaded when the local copy is missing or fails its
# published SHA256, so a warm actions/cache entry skips the download entirely.
# A candidate binary is accepted only after it has actually printed the expected
# OpenSCAD version, which proves the whole invocation path works before the
# test suite is allowed to run.
set -euo pipefail

version="${OPENSCAD_VERSION:?OPENSCAD_VERSION must be set}"
asset="${OPENSCAD_ASSET:?OPENSCAD_ASSET must be set}"
base_url="${OPENSCAD_BASE_URL:?OPENSCAD_BASE_URL must be set}"

workdir="${OPENSCAD_CACHE_DIR:-${RUNNER_TEMP:-/tmp}/openscad-${version}}"
url="${base_url}/${asset}"
sha_asset="${asset}.sha256"

mkdir -p "${workdir}"
cd "${workdir}"

sha_matches() {
  [ -f "${asset}" ] && [ -f "${sha_asset}" ] || return 1
  [ "$(awk '{print $1}' "${sha_asset}")" = "$(sha256sum "${asset}" | awk '{print $1}')" ]
}

if sha_matches; then
  echo "Reusing cached ${asset}"
else
  echo "::group::Download ${asset}"
  rm -f "${asset}"
  curl -fsSL --retry 3 -o "${asset}" "${url}"
  curl -fsSL --retry 3 -o "${sha_asset}" "${url}.sha256"
  echo "::endgroup::"
  if ! sha_matches; then
    echo "::error::SHA256 mismatch for ${asset}"
    exit 1
  fi
fi
echo "Verified ${asset} (sha256 $(awk '{print $1}' "${sha_asset}"))"

# FUSE is not available on GitHub runners, so the AppImage is extracted instead
# of mounted. `--appimage-extract` keeps the bundled libraries next to the
# binary, which is what makes the extracted `usr/bin/openscad` runnable.
root="${workdir}/squashfs-root"
if [ ! -d "${root}" ]; then
  echo "::group::Extract ${asset}"
  chmod +x "${asset}"
  "./${asset}" --appimage-extract >/dev/null
  echo "::endgroup::"
fi

mapfile -t candidates < <(
  find "${root}" \( -type f -o -type l \) \
    \( -name 'openscad' -o -name 'openscad.com' -o -name 'AppRun' \) -perm -u+x | sort -r
)
if [ "${#candidates[@]}" -eq 0 ]; then
  echo "::error::No OpenSCAD candidate inside ${root}"
  find "${root}" -maxdepth 3 | sort | head -50
  exit 1
fi

# Prints the version a candidate reports, or fails. `openscad --version` writes
# to stdout on every release line the adapter supports.
probe() {
  local executable="$1"
  local reported
  local probe_log="${workdir}/probe-$(basename "${executable}").log"
  # OpenSCAD links Qt, so even `--version` starts a GUI application: without a
  # display it exits before printing anything. xvfb is the CI-side display.
  local runner=()
  if command -v xvfb-run >/dev/null 2>&1; then
    runner=(xvfb-run -a)
  fi
  if ! reported="$(
    timeout 180 "${runner[@]}" "${executable}" --version 2>"${probe_log}" | head -1
  )"; then
    echo "candidate exited non-zero: ${executable}" >&2
    tail -20 "${probe_log}" >&2
    return 1
  fi
  reported="$(printf '%s' "${reported}" | tr -d '\r' | sed -n 's/^OpenSCAD version //p')"
  if [ -z "${reported}" ]; then
    echo "candidate reported no OpenSCAD version: ${executable}" >&2
    return 1
  fi
  printf '%s' "${reported}"
}

for candidate in "${candidates[@]}"; do
  echo "Probing ${candidate}"
  if ! reported="$(probe "${candidate}")"; then
    continue
  fi
  if [ "${reported}" != "${version}" ]; then
    echo "candidate reported OpenSCAD ${reported}, expected ${version}" >&2
    continue
  fi
  echo "Resolved OpenSCAD ${candidate} (OpenSCAD ${reported})"
  {
    echo "OPENSCAD_TEST_EXECUTABLE=${candidate}"
    echo "OPENSCAD_REAL_VERSION=${reported}"
  } >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
  exit 0
done

echo "::error::No candidate could run OpenSCAD ${version}"
exit 1
