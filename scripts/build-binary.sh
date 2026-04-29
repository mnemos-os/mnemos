#!/usr/bin/env bash
set -euo pipefail

if ! command -v pyinstaller >/dev/null 2>&1; then
  echo "ERROR: pyinstaller is required. Install with: pip install -e '.[build]'" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

detect_platform() {
  local kernel machine
  kernel="$(uname -s)"
  machine="$(uname -m)"

  case "${kernel}:${machine}" in
    Linux:x86_64|Linux:amd64)
      printf '%s\n' "linux-x86_64"
      ;;
    Linux:aarch64|Linux:arm64)
      printf '%s\n' "linux-aarch64"
      ;;
    Darwin:arm64)
      printf '%s\n' "macos-aarch64"
      ;;
    *)
      echo "ERROR: unsupported build host platform: ${kernel} ${machine}" >&2
      exit 2
      ;;
  esac
}

PLATFORM="${MNEMOS_BINARY_PLATFORM:-$(detect_platform)}"
if [[ -n "${MNEMOS_EXPECTED_PLATFORM:-}" && "${PLATFORM}" != "${MNEMOS_EXPECTED_PLATFORM}" ]]; then
  echo "ERROR: expected ${MNEMOS_EXPECTED_PLATFORM} build host, detected ${PLATFORM}" >&2
  exit 2
fi
BINARY="dist/mnemos-${PLATFORM}"

mkdir -p dist
rm -f "${BINARY}"

echo "[build-binary] building ${BINARY}"
export MNEMOS_BINARY_PLATFORM="${PLATFORM}"
export PYTHONOPTIMIZE=2
pyinstaller --clean --noconfirm --distpath dist --workpath build mnemos.spec

if [[ ! -x "${BINARY}" ]]; then
  echo "ERROR: expected executable not found at ${BINARY}" >&2
  exit 3
fi

echo "[build-binary] verifying version command"
"./${BINARY}" version

if [[ -n "${MNEMOS_BASE:-}" ]]; then
  echo "[build-binary] running health smoke against ${MNEMOS_BASE}"
  "./${BINARY}" health
else
  echo "[build-binary] MNEMOS_BASE not set; skipping health smoke"
fi

echo "[build-binary] built ./${BINARY}"
