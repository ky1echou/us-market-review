from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .fetch_market import load_config
from .send_report import PushResult, send_telegram_message, telegram_enabled


TIMEOUT_MESSAGE = "us-market-review 运行超时，未生成正式美股复盘，请检查数据源。"


def compact_list(values: list[Any], limit: int = 30) -> str:
    clean = [str(value) for value in values if str(value)]
    if not clean:
        return "无"
    if len(clean) <= limit:
        return ", ".join(clean)
    return ", ".join(clean[:limit]) + f" 等 {len(clean)} 项"


def load_run_state(path: str | Path) -> dict[str, Any]:
    state_path = Path(path)
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build_timeout_message(config: dict[str, Any], timeout_value: str) -> str:
    market = config.get("market", {})
    state_path = market.get("run_state_path") or os.getenv("MARKET_RUN_STATE_PATH") or "data/processed/market_run_state.json"
    state = load_run_state(state_path)
    return "\n".join(
        [
            TIMEOUT_MESSAGE,
            f"外层超时限制: {timeout_value}",
            f"已成功 ticker: {compact_list(state.get('success_tickers', []))}",
            f"未完成 ticker: {compact_list(state.get('pending_tickers', []))}",
            f"当前 provider: {state.get('current_provider') or '未披露'}",
            f"当前 ticker: {state.get('current_ticker') or '未披露'}",
            f"失败原因: {state.get('last_error') or state.get('reason') or 'external_timeout'}",
            f"状态更新时间: {state.get('updated_at') or '未披露'}",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a Telegram alert when run_daily.sh is killed by the external timeout.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"))
    parser.add_argument("--timeout", default=os.getenv("RUN_DAILY_TIMEOUT", "unknown"))
    args = parser.parse_args()
    load_dotenv()
    config = load_config(args.config)
    message = build_timeout_message(config, args.timeout)
    if telegram_enabled():
        result = send_telegram_message(message)
    else:
        result = PushResult("telegram", False, False, "disabled")
    print(f"timeout_alert telegram success={result.success} enabled={result.enabled} detail={result.detail}")


if __name__ == "__main__":
    main()
