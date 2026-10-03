#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PATH="${PROJECT_ROOT}/.venv"
CONFIG_PATH="${PROJECT_ROOT}/config/demo.toml"
DATASET_PATH="${PROJECT_ROOT}/data/advanced_siem/raw/advanced_siem_dataset.jsonl"

SKIP_IMPORT=0
RUNTIME_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./scripts/setup.sh [options]

Create the Python 3.12 environment, install locked dependencies, download and
verify the pinned SIEM dataset, validate configuration, and import into SQLite.

Options:
  --skip-import   Do not import the dataset into SQLite.
  --runtime-only  Install requirements.lock instead of development dependencies.
  -h, --help      Show this help message.
EOF
}

while (($#)); do
  case "$1" in
    --skip-import)
      SKIP_IMPORT=1
      ;;
    --runtime-only)
      RUNTIME_ONLY=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

cd "${PROJECT_ROOT}"

if [[ ! -x "${VENV_PATH}/bin/python" ]]; then
  if command -v uv >/dev/null 2>&1; then
    echo "[setup] Creating Python 3.12 environment with uv"
    uv venv --python 3.12 --seed "${VENV_PATH}"
  elif command -v python3.12 >/dev/null 2>&1; then
    echo "[setup] Creating Python 3.12 environment with venv"
    python3.12 -m venv "${VENV_PATH}"
  else
    echo "Python 3.12 is required. Install Python 3.12 or uv, then rerun setup." >&2
    exit 1
  fi
fi

PYTHON_VERSION="$("${VENV_PATH}/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "${PYTHON_VERSION}" != "3.12" ]]; then
  echo "${VENV_PATH} uses Python ${PYTHON_VERSION}; Python 3.12 is required." >&2
  echo "Move the existing environment aside and rerun this script." >&2
  exit 1
fi

LOCKFILE="requirements-dev.lock"
if ((RUNTIME_ONLY)); then
  LOCKFILE="requirements.lock"
fi

echo "[setup] Installing ${LOCKFILE}"
"${VENV_PATH}/bin/python" -m pip install --disable-pip-version-check -r "${LOCKFILE}"
"${VENV_PATH}/bin/python" -m pip install \
  --disable-pip-version-check \
  --no-deps \
  --no-build-isolation \
  -e .

mkdir -p "${PROJECT_ROOT}/var"
if [[ -f "${PROJECT_ROOT}/.env" ]]; then
  chmod 600 "${PROJECT_ROOT}/.env"
else
  cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
  chmod 600 "${PROJECT_ROOT}/.env"
  echo "[setup] Created .env from .env.example; add optional Slack credentials there."
fi

echo "[setup] Ensuring the pinned Hugging Face dataset is available"
"${VENV_PATH}/bin/clawwatch-demo" download-data --config "${CONFIG_PATH}"

echo "[setup] Validating configuration"
"${VENV_PATH}/bin/clawwatch-demo" config-check --config "${CONFIG_PATH}"

if ((SKIP_IMPORT)); then
  echo "[setup] Dataset import skipped"
elif [[ -f "${DATASET_PATH}" ]]; then
  echo "[setup] Importing dataset (safe to rerun)"
  "${VENV_PATH}/bin/clawwatch-demo" import-data --config "${CONFIG_PATH}"
else
  echo "Dataset not found at ${DATASET_PATH}" >&2
  echo "Download darkknight25/Advanced_SIEM_Dataset or rerun with --skip-import." >&2
  exit 1
fi

cat <<EOF

[setup] Complete
Run the dashboard:
  ${VENV_PATH}/bin/clawwatch-demo serve --config ${CONFIG_PATH}

Open:
  http://127.0.0.1:7860
EOF
