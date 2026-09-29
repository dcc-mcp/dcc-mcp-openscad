#!/usr/bin/env bash
# Install one pinned OpenSCAD build for the real-hardware CI job and export a
# verified executable path as OPENSCAD_TEST_EXECUTABLE.
#
# Two installation methods are supported:
#
#   OPENSCAD_METHOD=apt       Install the distribution package. Used for the
#                             2021.01 tier, which is exactly the version Ubuntu
#                             22.04 ships; the distribution build links against
#                             the runner's own libraries, so it avoids the
#                             AppImage's unbundled-GL problem entirely.
#   OPENSCAD_METHOD=appimage  Download a pinned AppImage, verify its SHA256 and
#                             extract it with --appimage-extract. Used for
#                             snapshot tiers no distribution package provides.
#                             AppImages do not bundle every GL library, so the
#                             runner must already have libEGL and friends
#                             installed (see the "Install OpenSCAD system
#                             libraries" step in ci.yml).
#
# Whichever method is used, a candidate is accepted only after it has actually
# printed an OpenSCAD version matching the requested tier. That proves the whole
# invocation path works before the test suite is allowed to run, and it stops a
# silently different version from being reported as the requested one.
#
# Required environment:
#   OPENSCAD_VERSION    expected version stamp, e.g. 2021.01 or 2026.09.29
#   OPENSCAD_METHOD     apt | appimage
#   OPENSCAD_ASSET      (appimage) file name of the AppImage
#   OPENSCAD_SHA256     (appimage) pinned SHA256 of that AppImage
#   OPENSCAD_ASSET_URL  (appimage) full download URL
#   GITHUB_ENV          (set by Actions) receives the resolved values
set -euo pipefail

: "${OPENSCAD_VERSION:?OPENSCAD_VERSION must be set}"
: "${OPENSCAD_METHOD:?OPENSCAD_METHOD must be set (apt or appimage)}"

# Runs a candidate and prints the version it reports.
# Diagnostics go to stderr because stdout is captured by the caller's $(...).
# On Linux the openscad binary is a Qt GUI application: it prints its version
# only once a display is available, and with no DISPLAY it exits 0 having
# written nothing. xvfb provides a headless display, so probes run under it
# whenever it is installed. This is why the version probe needs the same
# treatment the PNG render does.
run_probe_command() {
  local executable="$1"
  shift
  if command -v xvfb-run >/dev/null 2>&1; then
    timeout 180 xvfb-run -a "${executable}" "$@"
  else
    timeout 180 "${executable}" "$@"
  fi
}

probe() {
  local executable="$1"
  local output probe_err
  probe_err="$(mktemp)"
  if ! output="$(run_probe_command "${executable}" --version 2>"${probe_err}")"; then
    echo "candidate --version exited non-zero: ${executable}" >&2
    tail -20 "${probe_err}" >&2 || true
    rm -f "${probe_err}"
    return 1
  fi
  rm -f "${probe_err}"
  # Accept "OpenSCAD version 2021.01" and any prefix/suffix on that line.
  local version
  version="$(printf '%s' "${output}" | sed -n 's/.*OpenSCAD[[:space:]]*[Vv]ersion[[:space:]]*:*[[:space:]]*\([0-9][0-9.]*\).*/\1/p' | head -1)"
  if [ -z "${version}" ]; then
    # --info is the other way a build reports itself.
    local info
    if info="$(run_probe_command "${executable}" --info 2>/dev/null)"; then
      version="$(printf '%s' "${info}" | sed -n 's/.*OpenSCAD[[:space:]]*[Vv]ersion[[:space:]]*:*[[:space:]]*\([0-9][0-9.]*\).*/\1/p' | head -1)"
    fi
  fi
  if [ -z "${version}" ]; then
    echo "candidate reported no parseable version: ${executable} (got: ${output})" >&2
    return 1
  fi
  printf '%s' "${version}"
}

# Emits the resolved values and exits, or returns 1 to try the next candidate.
accept() {
  local executable="$1" reported="$2"
  if [ "${reported}" != "${OPENSCAD_VERSION}" ]; then
    echo "candidate reported OpenSCAD ${reported}, expected ${OPENSCAD_VERSION}" >&2
    return 1
  fi
  echo "Resolved OpenSCAD ${reported} via ${executable}"
  {
    echo "OPENSCAD_TEST_EXECUTABLE=${executable}"
    echo "OPENSCAD_REAL_VERSION=${reported}"
  } >> "${GITHUB_ENV:?GITHUB_ENV must be set}"
  exit 0
}

install_from_apt() {
  echo "::group::Install openscad from the distribution package"
  sudo apt-get update
  sudo apt-get install -y openscad
  echo "::endgroup::"

  local apt_version
  apt_version="$(dpkg-query --showformat='${Version}' --show openscad 2>/dev/null || true)"
  echo "apt installed openscad package version: ${apt_version:-unknown}"

  local candidate
  candidate="$(command -v openscad || true)"
  if [ -z "${candidate}" ]; then
    echo "::error::apt install finished but no openscad on PATH"
    exit 1
  fi
  local reported
  if ! reported="$(probe "${candidate}")"; then
    echo "::error::the apt-provided openscad could not report a version"
    exit 1
  fi
  # The package version carries a Debian revision; the stamp is what matters.
  if ! accept "${candidate}" "${reported}"; then
    echo "::error::apt provides OpenSCAD ${reported}, but this tier pins ${OPENSCAD_VERSION}"
    exit 1
  fi
}

install_from_appimage() {
  : "${OPENSCAD_ASSET:?OPENSCAD_ASSET must be set}"
  : "${OPENSCAD_SHA256:?OPENSCAD_SHA256 must be set}"
  : "${OPENSCAD_ASSET_URL:?OPENSCAD_ASSET_URL must be set}"

  local workdir="${OPENSCAD_CACHE_DIR:-${RUNNER_TEMP:-/tmp}/openscad-${OPENSCAD_VERSION}}"
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

  local root="${workdir}/squashfs-root"
  if [ ! -d "${root}" ]; then
    echo "::group::Extract ${OPENSCAD_ASSET}"
    chmod +x "${OPENSCAD_ASSET}"
    "./${OPENSCAD_ASSET}" --appimage-extract >/dev/null
    echo "::endgroup::"
  fi

  local candidates=()
  if [ -e "${root}/AppRun" ]; then
    candidates+=("${root}/AppRun")
  fi
  local found
  while IFS= read -r found; do
    candidates+=("${found}")
  done < <(find "${root}" \( -type f -o -type l \) -name 'openscad' -perm -u+x 2>/dev/null | sort)

  if [ "${#candidates[@]}" -eq 0 ]; then
    echo "::error::No openscad binary inside ${root}"
    find "${root}" -maxdepth 3 2>/dev/null | sort | head -50
    exit 1
  fi

  local candidate reported
  for candidate in "${candidates[@]}"; do
    echo "Probing ${candidate}"
    if reported="$(probe "${candidate}")"; then
      accept "${candidate}" "${reported}" || true
    fi
  done

  # Some AppImages only run correctly with the bundled libraries on the path.
  local libdirs wrapper
  libdirs="$(find "${root}" -type d \( -name 'lib' -o -name 'lib64' \) 2>/dev/null | tr '\n' ':')"
  for candidate in "${candidates[@]}"; do
    [ "${candidate}" = "${root}/AppRun" ] && continue
    wrapper="${workdir}/openscad-wrapper.sh"
    cat > "${wrapper}" <<WRAPPER
#!/usr/bin/env bash
export LD_LIBRARY_PATH="${libdirs}\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
exec "${candidate}" "\$@"
WRAPPER
    chmod +x "${wrapper}"
    echo "Probing ${candidate} via LD_LIBRARY_PATH wrapper"
    if reported="$(probe "${wrapper}")"; then
      accept "${wrapper}" "${reported}" || true
    fi
  done

  echo "::error::No candidate OpenSCAD could run and report ${OPENSCAD_VERSION}"
  exit 1
}

case "${OPENSCAD_METHOD}" in
  apt) install_from_apt ;;
  appimage) install_from_appimage ;;
  *)
    echo "::error::unknown OPENSCAD_METHOD: ${OPENSCAD_METHOD}"
    exit 1
    ;;
esac
