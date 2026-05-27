#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

read_env_value() {
  local key="$1"
  if [ -f ".env" ]; then
    grep -E "^${key}=" ".env" | tail -n 1 | cut -d "=" -f 2-
  fi
}

ENV_CONFIG_PATH="$(read_env_value CONFIG_PATH || true)"
ENV_LOG_FILE="$(read_env_value LOG_FILE || true)"
ENV_LOCK_FILE="$(read_env_value LOCK_FILE || true)"

CONFIG_PATH="${CONFIG_PATH:-${ENV_CONFIG_PATH:-config.yaml}}"
LOG_FILE="${LOG_FILE:-${ENV_LOG_FILE:-logs/daily.log}}"
LOCK_FILE="${LOCK_FILE:-${ENV_LOCK_FILE:-logs/run_daily.lock}}"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$LOCK_FILE")"

timestamp() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

echo "[$(timestamp)] run_daily.sh start project=$PROJECT_DIR config=$CONFIG_PATH" >> "$LOG_FILE"

if [ -n "${PYTHON:-}" ]; then
  PY_CMD=("$PYTHON")
elif [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
  PY_CMD=("$PROJECT_DIR/.venv/bin/python")
elif command -v python3 >/dev/null 2>&1; then
  PY_CMD=(python3)
elif command -v python >/dev/null 2>&1; then
  PY_CMD=(python)
else
  echo "[$(timestamp)] failure reason=Python not found. Create .venv or install python3." >> "$LOG_FILE"
  exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
  echo "[$(timestamp)] failure reason=Config file not found: $CONFIG_PATH" >> "$LOG_FILE"
  exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[$(timestamp)] skipped reason=another run_daily.sh is already running" >> "$LOG_FILE"
  exit 0
fi

set +e
"${PY_CMD[@]}" -m src.render_report --config "$CONFIG_PATH" 2>&1 | tee -a "$LOG_FILE"
status=${PIPESTATUS[0]}
set -e

if [ "$status" -eq 0 ]; then
  echo "[$(timestamp)] run_daily.sh finish status=success" >> "$LOG_FILE"
else
  echo "[$(timestamp)] run_daily.sh finish status=failed exit_code=$status" >> "$LOG_FILE"
fi

exit "$status"
