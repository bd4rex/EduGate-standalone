#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-python3}"
PYPI_INDEX="${PYPI_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
EDUGATE_VERSION="${EDUGATE_VERSION:-0.0.0}"
BUILD_VENV="${PWD}/.venv-build-macos"
BUILD_PYTHON="${BUILD_VENV}/bin/python"

"${PYTHON_BIN}" -c 'import operator,sys; raise SystemExit(0 if operator.ge(sys.version_info, (3, 10)) else 1)' || {
  echo "[ERROR] EduGate builds require Python 3.10 or newer." >&2
  exit 1
}

if [[ "$("${PYTHON_BIN}" -c 'import platform; print(platform.machine())')" != "arm64" ]]; then
  echo "[ERROR] This release script currently builds the macOS Apple Silicon arm64 package." >&2
  exit 1
fi

if [[ ! -x "${BUILD_PYTHON}" ]]; then
  "${PYTHON_BIN}" -m venv "${BUILD_VENV}"
fi

"${BUILD_PYTHON}" -m pip install --index-url "${PYPI_INDEX}" --upgrade pip pyinstaller
"${BUILD_PYTHON}" -m pip install --index-url "${PYPI_INDEX}" -r backend/requirements.txt

EDUGATE_VERSION="${EDUGATE_VERSION}" \
  "${BUILD_PYTHON}" -m PyInstaller --noconfirm --clean desktop/edugate_standalone.spec

APP_PATH="${PWD}/dist/EduGate.app"
if [[ ! -d "${APP_PATH}" ]]; then
  echo "[ERROR] PyInstaller did not create ${APP_PATH}." >&2
  exit 1
fi

/usr/bin/xattr -cr "${APP_PATH}"
codesign --force --deep --sign - --timestamp=none "${APP_PATH}"
codesign --verify --deep --strict --verbose=2 "${APP_PATH}"

STAGE_NAME="EduGate-Standalone-v${EDUGATE_VERSION}-macos-arm64"
STAGE_PATH="${PWD}/dist/${STAGE_NAME}"
ARCHIVE_PATH="${PWD}/dist/${STAGE_NAME}.zip"
PREVIOUS_DIR="${PWD}/build/previous-macos-builds/$(date +%Y%m%d-%H%M%S)"
for previous in "${STAGE_PATH}" "${ARCHIVE_PATH}"; do
  if [[ -e "${previous}" ]]; then
    mkdir -p "${PREVIOUS_DIR}"
    mv "${previous}" "${PREVIOUS_DIR}/"
  fi
done
mkdir -p "${STAGE_PATH}"
ditto --norsrc --noextattr --noqtn --noacl "${APP_PATH}" "${STAGE_PATH}/EduGate.app"
cp desktop/MACOS-README.txt "${STAGE_PATH}/安装说明.txt"
/usr/bin/xattr -cr "${STAGE_PATH}"
codesign --verify --deep --strict --verbose=2 "${STAGE_PATH}/EduGate.app"
ditto -c -k --norsrc --noextattr --noqtn --noacl --keepParent "${STAGE_PATH}" "${ARCHIVE_PATH}"

echo
echo "[OK] macOS Apple Silicon bundle created:"
echo "  ${ARCHIVE_PATH}"
shasum -a 256 "${ARCHIVE_PATH}"
