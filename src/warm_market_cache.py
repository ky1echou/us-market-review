from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .fetch_market import fetch_market_data, load_config
from .macro_proxy import apply_macro_proxies


def run(config_path: str) -> dict:
    config = load_config(config_path)
    market = config.setdefault("market", {})
    market["prefer_cache"] = False
    market["period"] = str(market.get("warm_cache_period", "90d"))
    market["request_delay_sec"] = max(float(market.get("request_delay_sec", 0.5)), 0.5)
    market_data = apply_macro_proxies(config, fetch_market_data(config))
    output_path = Path(market.get("latest_data_path", "data/processed/latest_market_data.json"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(market_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    metadata = market_data.get("metadata", {})
    macro_proxy = ",".join(metadata.get("macro_proxy_tickers", [])) or "none"
    print(
        "warm_market_cache "
        f"success={metadata.get('success_count', 0)}/{metadata.get('total_count', 0)} "
        f"live={metadata.get('live_success_count', 0)} "
        f"cache={metadata.get('cache_success_count', 0)} "
        f"failed={metadata.get('failed_count', 0)} "
        f"macro_proxy={macro_proxy} "
        f"failed_tickers={','.join(metadata.get('failed_tickers', []))}"
    )
    return market_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Warm local market cache by slowly refreshing the full formal universe.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"))
    args = parser.parse_args()
    run(args.config)


if __name__ == "__main__":
    main()
