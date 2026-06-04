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
ENV_RUN_TIMEOUT="$(read_env_value RUN_DAILY_TIMEOUT || true)"

CONFIG_PATH="${CONFIG_PATH:-${ENV_CONFIG_PATH:-config.yaml}}"
LOG_FILE="${LOG_FILE:-${ENV_LOG_FILE:-logs/daily.log}}"
LOCK_FILE="${LOCK_FILE:-${ENV_LOCK_FILE:-logs/run_daily.lock}}"
LOCK_PID_FILE="${LOCK_FILE}.pid"
RUN_DAILY_TIMEOUT="${RUN_DAILY_TIMEOUT:-${ENV_RUN_TIMEOUT:-45m}}"

mkdir -p "$(dirname "$LOG_FILE")"
mkdir -p "$(dirname "$LOCK_FILE")"

timestamp() {
  date +"%Y-%m-%dT%H:%M:%S%z"
}

echo "[$(timestamp)] run_daily.sh start project=$PROJECT_DIR config=$CONFIG_PATH timeout=$RUN_DAILY_TIMEOUT" >> "$LOG_FILE"

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

notify_skip() {
  "${PY_CMD[@]}" - <<'PY' >> "$LOG_FILE" 2>&1 || true
from src.send_report import send_telegram_message, telegram_enabled
message = "us-market-review 正在运行，已跳过本次触发。"
if telegram_enabled():
    result = send_telegram_message(message)
    print(f"skip_notify telegram success={result.success} detail={result.detail}")
else:
    print("skip_notify telegram disabled")
PY
}

if [ ! -f "$CONFIG_PATH" ]; then
  echo "[$(timestamp)] failure reason=Config file not found: $CONFIG_PATH" >> "$LOG_FILE"
  exit 1
fi

acquire_lock() {
  exec 9>"$LOCK_FILE"
  flock -n 9
}

if ! acquire_lock; then
  existing_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    existing_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi

  if [ -n "$existing_pid" ] && ! kill -0 "$existing_pid" 2>/dev/null; then
    echo "[$(timestamp)] stale lock detected pid=$existing_pid; clearing lock files" >> "$LOG_FILE"
    rm -f "$LOCK_FILE" "$LOCK_PID_FILE"
    if ! acquire_lock; then
      echo "[$(timestamp)] skipped reason=another run_daily.sh is already running after stale cleanup" >> "$LOG_FILE"
      notify_skip
      exit 0
    fi
  else
    echo "[$(timestamp)] skipped reason=another run_daily.sh is already running pid=${existing_pid:-unknown}" >> "$LOG_FILE"
    notify_skip
    exit 0
  fi
fi

echo "$$" > "$LOCK_PID_FILE"
cleanup() {
  rm -f "$LOCK_PID_FILE"
}
trap cleanup EXIT

set +e
if command -v timeout >/dev/null 2>&1; then
  timeout "$RUN_DAILY_TIMEOUT" "${PY_CMD[@]}" -m src.render_report --config "$CONFIG_PATH" 2>&1 | tee -a "$LOG_FILE"
  status=${PIPESTATUS[0]}
else
  echo "[$(timestamp)] warning reason=timeout command not found; running without external timeout" >> "$LOG_FILE"
  "${PY_CMD[@]}" -m src.render_report --config "$CONFIG_PATH" 2>&1 | tee -a "$LOG_FILE"
  status=${PIPESTATUS[0]}
fi
set -e

if [ "$status" -eq 0 ]; then
  echo "[$(timestamp)] run_daily.sh finish status=success" >> "$LOG_FILE"
elif [ "$status" -eq 124 ]; then
  echo "[$(timestamp)] run_daily.sh finish status=failed reason=timeout timeout=$RUN_DAILY_TIMEOUT" >> "$LOG_FILE"
else
  echo "[$(timestamp)] run_daily.sh finish status=failed exit_code=$status" >> "$LOG_FILE"
fi

exit "$status"
