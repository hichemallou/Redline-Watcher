#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${PROJECT_ROOT}/.venv/bin/clawwatch-demo"
DEFAULT_CONFIG="${PROJECT_ROOT}/config/demo.toml"

usage() {
  cat <<'EOF'
Usage: ./scripts/start_gradio.sh [--config PATH]

Start the ClawWatch Gradio dashboard in the foreground. The default configuration
is config/demo.toml. Stop the server with Ctrl+C.
EOF
}

CONFIG_PATH="${DEFAULT_CONFIG}"
while (($#)); do
  case "$1" in
    --config)
      if (($# < 2)); then
        echo "--config requires a path" >&2
        exit 2
      fi
      CONFIG_PATH="$2"
      shift
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

if [[ ! -x "${APP}" ]]; then
  echo "Application environment not found. Run ./scripts/setup.sh first." >&2
  exit 1
fi

if [[ "${CONFIG_PATH}" != /* ]]; then
  CONFIG_PATH="${PROJECT_ROOT}/${CONFIG_PATH}"
fi
if [[ ! -f "${CONFIG_PATH}" ]]; then
  echo "Configuration file not found: ${CONFIG_PATH}" >&2
  exit 1
fi

cd "${PROJECT_ROOT}"
echo "Starting ClawWatch from ${CONFIG_PATH}"
exec "${APP}" serve --config "${CONFIG_PATH}"
