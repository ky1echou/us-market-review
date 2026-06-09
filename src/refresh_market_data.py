from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .fetch_market import fetch_market_data, load_config


def latest_market_data_path(config: dict) -> Path:
    return Path(config.get("market", {}).get("latest_data_path", "data/processed/latest_market_data.json"))


def run(config_path: str, output: str | None = None) -> Path:
    config = load_config(config_path)
    config.setdefault("market", {})["prefer_cache"] = False
    market_data = fetch_market_data(config)
    output_path = Path(output) if output else latest_market_data_path(config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(market_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    metadata = market_data.get("metadata", {})
    print(
        "refresh_market_data "
        f"success={metadata.get('success_count', 0)}/{metadata.get('total_count', 0)} "
        f"live={metadata.get('live_success_count', 0)} "
        f"cache={metadata.get('cache_success_count', 0)} "
        f"failed={metadata.get('failed_count', 0)} "
        f"output={output_path}"
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh latest market data before report generation.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    run(args.config, args.output or None)


if __name__ == "__main__":
    main()
