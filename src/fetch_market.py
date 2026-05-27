from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
import yfinance as yf
from dotenv import load_dotenv

from .indicators import summarize_price_frame


DATA_SOURCE = "Yahoo Finance via yfinance"


def load_config(config_path: str | Path) -> dict[str, Any]:
    load_dotenv()
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file) or {}

    config.setdefault("app", {})
    config.setdefault("market", {})
    config.setdefault("report", {})

    if os.getenv("APP_TIMEZONE"):
        config["app"]["timezone"] = os.getenv("APP_TIMEZONE")
    if os.getenv("OUTPUT_DIR"):
        config["app"]["output_dir"] = os.getenv("OUTPUT_DIR")
    if os.getenv("ENABLE_PDF"):
        config.setdefault("report", {}).setdefault("pdf", {})
        config["report"]["pdf"]["enabled"] = os.getenv("ENABLE_PDF", "true").lower() == "true"
    if os.getenv("REPORT_FONT_PATH"):
        config.setdefault("report", {}).setdefault("pdf", {})
        config["report"]["pdf"]["font_path"] = os.getenv("REPORT_FONT_PATH")

    return config


def now_iso(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).replace(microsecond=0).isoformat()


def universe_from_config(config: dict[str, Any]) -> list[dict[str, str]]:
    market = config.get("market", {})
    groups = [
        ("benchmarks", "index"),
        ("sector_etfs", "sector_etf"),
        ("key_stocks", "key_stock"),
        ("macro_assets", "macro_asset"),
    ]

    seen: set[str] = set()
    universe: list[dict[str, str]] = []
    for key, category in groups:
        for item in market.get(key, []):
            ticker = str(item.get("ticker", "")).strip()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            universe.append(
                {
                    "ticker": ticker,
                    "name": str(item.get("name") or ticker),
                    "category": category,
                    "theme": str(item.get("theme") or ""),
                }
            )
    return universe


def fetch_history(ticker: str, period: str, interval: str) -> tuple[pd.DataFrame, str | None]:
    try:
        frame = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    except Exception as exc:  # noqa: BLE001 - keep the report running when one ticker fails.
        return pd.DataFrame(), str(exc)

    if frame is None or frame.empty:
        return pd.DataFrame(), "empty price history"

    frame = frame.reset_index()
    date_column = "Date" if "Date" in frame.columns else "Datetime"
    frame[date_column] = pd.to_datetime(frame[date_column])
    frame = frame.set_index(date_column).sort_index()
    return frame, None


def fetch_market_data(config: dict[str, Any]) -> dict[str, Any]:
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    market = config.get("market", {})
    period = market.get("period", "90d")
    interval = market.get("interval", "1d")
    fetched_at = now_iso(timezone_name)

    assets: list[dict[str, Any]] = []
    for asset in universe_from_config(config):
        frame, error = fetch_history(asset["ticker"], period, interval)
        summary = summarize_price_frame(frame)
        source = {
            "provider": DATA_SOURCE,
            "ticker": asset["ticker"],
            "period": period,
            "interval": interval,
            "as_of": summary.get("as_of"),
            "fetched_at": fetched_at,
        }
        assets.append(
            {
                **asset,
                **summary,
                "source": source,
                "error": error,
            }
        )

    return {
        "metadata": {
            "source": DATA_SOURCE,
            "period": period,
            "interval": interval,
            "fetched_at": fetched_at,
            "timezone": timezone_name,
        },
        "assets": assets,
    }


def assets_by_category(market_data: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [asset for asset in market_data.get("assets", []) if asset.get("category") == category]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch yfinance market data and print a JSON snapshot.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"))
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    config = load_config(args.config)
    market_data = fetch_market_data(config)
    payload = json.dumps(market_data, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()

