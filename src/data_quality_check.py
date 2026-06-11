from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .fetch_market import load_config


def latest_market_data_path(config: dict[str, Any]) -> Path:
    return Path(config.get("market", {}).get("latest_data_path", "data/processed/latest_market_data.json"))


def compact(values: list[Any], limit: int = 40) -> str:
    items = [str(value) for value in values if str(value)]
    if not items:
        return "无"
    if len(items) <= limit:
        return ",".join(items)
    return ",".join(items[:limit]) + f" 等{len(items)}项"


def run(config_path: str, input_path: str | None = None) -> dict[str, Any]:
    config = load_config(config_path)
    path = Path(input_path) if input_path else latest_market_data_path(config)
    if not path.exists():
        raise FileNotFoundError(f"latest market data snapshot not found: {path}")
    market_data = json.loads(path.read_text(encoding="utf-8"))
    metadata = market_data.get("metadata", {})
    total = int(metadata.get("total_count") or 0)
    success = int(metadata.get("success_count") or 0)
    live = int(metadata.get("live_success_count") or 0)
    cache = int(metadata.get("cache_success_count") or 0)
    failed = int(metadata.get("failed_count") or max(total - success, 0))
    print(
        "data_quality_check "
        f"success={success}/{total} "
        f"live={live} cache={cache} failed={failed} "
        f"formal_report_allowed={metadata.get('formal_report_allowed')} "
        f"macro_proxy={compact(metadata.get('macro_proxy_tickers', []))} "
        f"failed_tickers={compact(metadata.get('failed_tickers', []))}"
    )
    return market_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Print a lightweight quality summary for the refreshed market data snapshot.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"))
    parser.add_argument("--input", default="")
    args = parser.parse_args()
    run(args.config, args.input or None)


if __name__ == "__main__":
    main()
