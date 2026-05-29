from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .fetch_market import load_config, parse_provider_chain
from .market_data_provider import provider_display_name


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a lightweight deployment health check without fetching market data.")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = load_config(args.config)
    market = config.get("market", {})
    provider_chain = parse_provider_chain(market.get("provider_chain", market.get("provider")))
    provider_names = []
    for provider in provider_chain:
        provider_names.append(provider_display_name(provider))

    output_root = Path(config.get("app", {}).get("output_dir", "reports"))
    required_dirs = [
        Path("logs"),
        output_root / "markdown",
        output_root / "pdf",
        output_root / "html",
        Path("data/raw"),
        Path("data/processed"),
        Path(market.get("cache_dir", "data/processed/market_cache")),
    ]
    for directory in required_dirs:
        directory.mkdir(parents=True, exist_ok=True)

    print("health_check=ok")
    print(f"config={args.config}")
    print(f"provider_chain={' -> '.join(provider_names)}")
    print(f"wkhtmltopdf={shutil.which('wkhtmltopdf') or 'not_found'}")
    print("note=no market data fetched in health check")


if __name__ == "__main__":
    main()
