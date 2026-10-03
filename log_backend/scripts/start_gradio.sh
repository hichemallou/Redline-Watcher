#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP="${PROJECT_ROOT}/.venv/bin/clawwatch-demo"
DEFAULT_CONFIG="${PROJECT_ROOT}/config/demo.toml"

usage() {
  cat <<'EOF'
Usage: ./scripts/start_gradio.sh [--config PATH] [--no-auto-send-critical]

Start the ClawWatch Gradio dashboard in the foreground. The default configuration
is config/demo.toml. Stop the server with Ctrl+C.
Critical replay logs are sent automatically through NemoClaw on this host by default.
Use --no-auto-send-critical to start with sending disabled (for example, locally).
EOF
}

CONFIG_PATH="${DEFAULT_CONFIG}"
EXTRA_ARGS=()
while (($#)); do
  case "$1" in
    --auto-send-critical|--no-auto-send-critical)
      EXTRA_ARGS+=("$1")
      ;;
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
exec "${APP}" serve --config "${CONFIG_PATH}" "${EXTRA_ARGS[@]}"
