from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
from dotenv import load_dotenv

from .indicators import summarize_price_frame
from .market_data_provider import make_provider, provider_display_name


DEFAULT_PROVIDER_CHAIN = ["yfinance", "stooq"]


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_provider_chain(value: Any) -> list[str]:
    if value is None or value == "":
        return DEFAULT_PROVIDER_CHAIN.copy()
    if isinstance(value, list):
        providers = [str(item).strip() for item in value if str(item).strip()]
    else:
        providers = [item.strip() for item in str(value).split(",") if item.strip()]
    providers = providers or DEFAULT_PROVIDER_CHAIN.copy()
    if [provider.lower() for provider in providers] == ["yfinance"]:
        return DEFAULT_PROVIDER_CHAIN.copy()
    return providers


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

    market = config.setdefault("market", {})
    if os.getenv("MARKET_PROVIDER_CHAIN"):
        market["provider_chain"] = parse_provider_chain(os.getenv("MARKET_PROVIDER_CHAIN"))
    elif os.getenv("MARKET_PROVIDER"):
        market["provider_chain"] = parse_provider_chain(os.getenv("MARKET_PROVIDER", "yfinance"))
    if os.getenv("MARKET_REQUEST_DELAY_SEC"):
        market["request_delay_sec"] = env_float("MARKET_REQUEST_DELAY_SEC", float(market.get("request_delay_sec", 1.0)))
    if os.getenv("MARKET_RETRY_COUNT"):
        market["retry_count"] = env_int("MARKET_RETRY_COUNT", int(market.get("retry_count", 1)))
    if os.getenv("MARKET_RETRY_BACKOFF_SEC"):
        market["retry_backoff_sec"] = env_float("MARKET_RETRY_BACKOFF_SEC", float(market.get("retry_backoff_sec", 4.0)))
    if os.getenv("MARKET_CACHE_DIR"):
        market["cache_dir"] = os.getenv("MARKET_CACHE_DIR")
    if os.getenv("MARKET_CACHE_MAX_AGE_HOURS"):
        market["cache_max_age_hours"] = env_int("MARKET_CACHE_MAX_AGE_HOURS", int(market.get("cache_max_age_hours", 168)))
    if os.getenv("MARKET_MIN_SUCCESS_RATIO"):
        market["min_success_ratio"] = env_float("MARKET_MIN_SUCCESS_RATIO", float(market.get("min_success_ratio", 0.7)))

    if os.getenv("ENABLE_PDF"):
        config.setdefault("report", {}).setdefault("pdf", {})
        config["report"]["pdf"]["enabled"] = os.getenv("ENABLE_PDF", "true").lower() == "true"
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


def safe_cache_name(ticker: str, period: str, interval: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in ticker)
    return f"{cleaned}_{period}_{interval}.csv"


def cache_path(cache_dir: str | Path, ticker: str, period: str, interval: str) -> Path:
    return Path(cache_dir) / safe_cache_name(ticker, period, interval)


def save_cache(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index_label="Date")


def load_cache(path: Path, max_age_hours: int) -> tuple[pd.DataFrame, str | None]:
    if not path.exists():
        return pd.DataFrame(), None

    age = datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)
    if age > timedelta(hours=max_age_hours):
        return pd.DataFrame(), f"cache exists but is older than {max_age_hours} hours"

    try:
        frame = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
    except Exception as exc:  # noqa: BLE001 - bad cache should not break the run.
        return pd.DataFrame(), f"cache read failed: {exc}"

    if frame.empty:
        return pd.DataFrame(), "cache is empty"
    return frame, None


def fetch_history_with_provider_chain(
    ticker: str,
    period: str,
    interval: str,
    provider_chain: list[str],
    cache_dir: str | Path,
    retry_count: int,
    retry_backoff_sec: float,
    cache_max_age_hours: int,
) -> tuple[pd.DataFrame, str | None, bool, str | None, str]:
    errors: list[str] = []
    for provider_name in provider_chain:
        try:
            provider = make_provider(provider_name)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider_name}: {exc}")
            continue

        for attempt in range(1, retry_count + 1):
            try:
                frame = provider.fetch_history(ticker, period, interval)
                if frame is None or frame.empty:
                    raise ValueError("empty price history")
                save_cache(frame, cache_path(cache_dir, ticker, period, interval))
                return frame, None, False, None, provider.name
            except Exception as exc:  # noqa: BLE001 - try next provider, then cache.
                errors.append(f"{provider.name} attempt {attempt}/{retry_count}: {exc}")
                if attempt < retry_count:
                    time.sleep(retry_backoff_sec * attempt)

    cached_frame, cache_error = load_cache(cache_path(cache_dir, ticker, period, interval), cache_max_age_hours)
    if not cached_frame.empty:
        return cached_frame, "; ".join(errors), True, None, "Local market cache"

    error_text = "; ".join(errors)
    if cache_error:
        error_text = f"{error_text}; cache fallback failed: {cache_error}"
    else:
        error_text = f"{error_text}; no cache fallback available"
    return pd.DataFrame(), error_text, False, cache_error, ""


def build_market_quality(assets: list[dict[str, Any]], source: str, fetched_at: str, min_success_ratio: float) -> dict[str, Any]:
    total = len(assets)
    usable = [asset for asset in assets if asset.get("last_close") is not None]
    live_success = [asset for asset in usable if not asset.get("from_cache")]
    cache_success = [asset for asset in usable if asset.get("from_cache")]
    failed = total - len(usable)
    success_ratio = len(usable) / total if total else 0.0
    live_success_ratio = len(live_success) / total if total else 0.0
    provider_counts: dict[str, int] = {}
    for asset in usable:
        provider = str(asset.get("source", {}).get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1

    warnings: list[str] = []
    formal_report_allowed = live_success_ratio >= min_success_ratio
    if not formal_report_allowed:
        warnings.append("今日行情数据抓取失败，未生成正式美股复盘，请检查数据源。")
    elif success_ratio < min_success_ratio:
        warnings.append("行情数据不完整，请谨慎使用")

    return {
        "source": source,
        "fetched_at": fetched_at,
        "total_count": total,
        "success_count": len(usable),
        "live_success_count": len(live_success),
        "cache_success_count": len(cache_success),
        "failed_count": failed,
        "success_ratio": success_ratio,
        "live_success_ratio": live_success_ratio,
        "min_success_ratio": min_success_ratio,
        "data_complete": success_ratio >= min_success_ratio,
        "live_data_complete": live_success_ratio >= min_success_ratio,
        "formal_report_allowed": formal_report_allowed,
        "provider_counts": provider_counts,
        "warnings": warnings,
    }


def provider_chain_source(provider_chain: list[str]) -> str:
    names: list[str] = []
    for provider in provider_chain:
        try:
            names.append(provider_display_name(provider))
        except Exception:  # noqa: BLE001
            names.append(provider)
    return " -> ".join(names)


def fetch_market_data(config: dict[str, Any]) -> dict[str, Any]:
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    market = config.get("market", {})
    period = market.get("period", "90d")
    interval = market.get("interval", "1d")
    provider_chain = parse_provider_chain(market.get("provider_chain", market.get("provider", DEFAULT_PROVIDER_CHAIN)))
    source = provider_chain_source(provider_chain)
    request_delay_sec = float(market.get("request_delay_sec", 1.0))
    retry_count = max(1, int(market.get("retry_count", 1)))
    retry_backoff_sec = float(market.get("retry_backoff_sec", 4.0))
    cache_dir = market.get("cache_dir", "data/processed/market_cache")
    cache_max_age_hours = int(market.get("cache_max_age_hours", 168))
    min_success_ratio = float(market.get("min_success_ratio", 0.7))
    fetched_at = now_iso(timezone_name)

    assets: list[dict[str, Any]] = []
    universe = universe_from_config(config)
    for index, asset in enumerate(universe):
        if index > 0 and request_delay_sec > 0:
            time.sleep(request_delay_sec)

        frame, error, from_cache, cache_error, actual_provider = fetch_history_with_provider_chain(
            asset["ticker"],
            period,
            interval,
            provider_chain,
            cache_dir,
            retry_count,
            retry_backoff_sec,
            cache_max_age_hours,
        )
        summary = summarize_price_frame(frame)
        source_info = {
            "provider": actual_provider or source,
            "provider_chain": source,
            "ticker": asset["ticker"],
            "period": period,
            "interval": interval,
            "as_of": summary.get("as_of"),
            "fetched_at": fetched_at,
            "from_cache": from_cache,
            "cache_path": str(cache_path(cache_dir, asset["ticker"], period, interval)) if from_cache else "",
        }
        assets.append(
            {
                **asset,
                **summary,
                "source": source_info,
                "error": error,
                "from_cache": from_cache,
                "cache_error": cache_error,
            }
        )

    quality = build_market_quality(assets, source, fetched_at, min_success_ratio)
    return {
        "metadata": {
            "source": source,
            "provider_chain": provider_chain,
            "period": period,
            "interval": interval,
            "fetched_at": fetched_at,
            "timezone": timezone_name,
            "request_delay_sec": request_delay_sec,
            "retry_count": retry_count,
            "retry_backoff_sec": retry_backoff_sec,
            "cache_dir": str(cache_dir),
            "cache_max_age_hours": cache_max_age_hours,
            **quality,
        },
        "assets": assets,
    }


def assets_by_category(market_data: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [asset for asset in market_data.get("assets", []) if asset.get("category") == category]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch market data and print a JSON snapshot.")
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
