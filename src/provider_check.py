from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .fetch_market import (
    cache_path,
    configured_provider_order,
    load_cache,
    load_config,
    provider_options_for,
)
from .market_data_provider import make_provider, provider_display_name, resolve_provider_symbol


CORE_TICKERS = ["SPY", "QQQ", "DIA", "IWM", "NVDA", "MSFT", "AAPL", "AMD", "AVGO", "TSLA"]
LIVE_PROVIDERS = ["yfinance", "stooq"]
ALL_PROVIDERS = ["yfinance", "stooq", "cache"]


def log_path() -> Path:
    path = Path("logs/provider_check.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def truncate_reason(reason: str, limit: int = 500) -> str:
    clean = " ".join(str(reason).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def parse_tickers(value: str | None) -> list[str]:
    if not value:
        return CORE_TICKERS.copy()
    tickers = [item.strip().upper() for item in value.split(",") if item.strip()]
    return tickers or CORE_TICKERS.copy()


def providers_to_check(provider: str) -> list[str]:
    normalized = provider.strip().lower()
    if normalized == "all":
        return ALL_PROVIDERS.copy()
    if normalized in ALL_PROVIDERS:
        return [normalized]
    raise ValueError("--provider must be one of: all, yfinance, stooq, cache")


def check_live_provider(
    provider_name: str,
    tickers: list[str],
    period: str,
    interval: str,
    provider_options: dict[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    success = 0
    failed = 0

    for ticker in tickers:
        symbol = resolve_provider_symbol(provider_name, ticker, provider_options_for(provider_options, provider_name))
        try:
            provider = make_provider(provider_name, provider_options_for(provider_options, provider_name))
            frame = provider.fetch_history(ticker, period, interval)
            if frame is None or frame.empty:
                raise ValueError("empty price history")
            success += 1
            rows.append(
                {
                    "ticker": ticker,
                    "symbol": symbol,
                    "status": "ok",
                    "rows": int(len(frame)),
                    "latest": str(frame.index.max()),
                    "reason": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic should continue.
            failed += 1
            rows.append(
                {
                    "ticker": ticker,
                    "symbol": symbol,
                    "status": "failed",
                    "rows": 0,
                    "latest": "",
                    "reason": truncate_reason(str(exc)),
                }
            )
    return {
        "provider": provider_display_name(provider_name),
        "provider_key": provider_name,
        "success_count": success,
        "failed_count": failed,
        "items": rows,
    }


def check_cache_provider(
    tickers: list[str],
    period: str,
    interval: str,
    cache_dir: str,
    cache_max_age_hours: int,
    cache_max_trading_days: int,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    success = 0
    failed = 0

    for ticker in tickers:
        path = cache_path(cache_dir, ticker, period, interval)
        frame, error = load_cache(path, cache_max_age_hours, cache_max_trading_days)
        if not frame.empty:
            success += 1
            rows.append(
                {
                    "ticker": ticker,
                    "symbol": str(path),
                    "status": "ok",
                    "rows": int(len(frame)),
                    "latest": str(frame.index.max()),
                    "reason": "",
                }
            )
        else:
            failed += 1
            rows.append(
                {
                    "ticker": ticker,
                    "symbol": str(path),
                    "status": "failed",
                    "rows": 0,
                    "latest": "",
                    "reason": truncate_reason(error or "no cache fallback available"),
                }
            )
    return {
        "provider": "Local market cache",
        "provider_key": "cache",
        "success_count": success,
        "failed_count": failed,
        "items": rows,
    }


def render_text(results: list[dict[str, Any]], generated_at: str) -> str:
    lines = [f"provider_check generated_at={generated_at}"]
    for result in results:
        lines.append(
            f"provider={result['provider']} success={result['success_count']} failed={result['failed_count']}"
        )
        for item in result["items"]:
            reason = f" reason={item['reason']}" if item.get("reason") else ""
            lines.append(
                f"  ticker={item['ticker']} symbol={item['symbol']} status={item['status']} rows={item['rows']} latest={item['latest']}{reason}"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Check yfinance, Stooq, and local cache with a small core ticker set.")
    parser.add_argument("--provider", default="all", help="all, yfinance, stooq, or cache")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker list. Defaults to 10 core tickers.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args()

    config = load_config(args.config)
    market = config.get("market", {})
    provider_options = market.get("provider_options", {}) if isinstance(market.get("provider_options", {}), dict) else {}
    tickers = parse_tickers(args.tickers)
    selected_providers = providers_to_check(args.provider)
    period = str(market.get("provider_check_period", "30d"))
    interval = str(market.get("interval", "1d"))
    cache_dir = str(market.get("cache_dir", "data/processed/market_cache"))
    cache_max_age_hours = int(market.get("cache_max_age_hours", 168))
    cache_max_trading_days = int(market.get("cache_max_trading_days", 3))
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    results: list[dict[str, Any]] = []
    for provider_name in selected_providers:
        if provider_name in LIVE_PROVIDERS:
            results.append(check_live_provider(provider_name, tickers, period, interval, provider_options))
        elif provider_name == "cache":
            results.append(check_cache_provider(tickers, period, interval, cache_dir, cache_max_age_hours, cache_max_trading_days))

    payload = {
        "generated_at": generated_at,
        "config": args.config,
        "configured_provider_order": configured_provider_order(market),
        "tickers": tickers,
        "results": results,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n" if args.json else render_text(results, generated_at)
    log_path().write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
