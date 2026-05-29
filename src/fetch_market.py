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
import yfinance as yf
from dotenv import load_dotenv

from .indicators import summarize_price_frame


DEFAULT_DATA_SOURCE = "Yahoo Finance via yfinance"


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
    if os.getenv("MARKET_PROVIDER"):
        market["provider"] = os.getenv("MARKET_PROVIDER")
    if os.getenv("MARKET_REQUEST_DELAY_SEC"):
        market["request_delay_sec"] = env_float("MARKET_REQUEST_DELAY_SEC", float(market.get("request_delay_sec", 2.0)))
    if os.getenv("MARKET_RETRY_COUNT"):
        market["retry_count"] = env_int("MARKET_RETRY_COUNT", int(market.get("retry_count", 3)))
    if os.getenv("MARKET_RETRY_BACKOFF_SEC"):
        market["retry_backoff_sec"] = env_float("MARKET_RETRY_BACKOFF_SEC", float(market.get("retry_backoff_sec", 10.0)))
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


def data_source_for_provider(provider: str) -> str:
    provider = provider.lower().strip()
    if provider == "yfinance":
        return DEFAULT_DATA_SOURCE
    return f"{provider} market data provider"


def safe_cache_name(ticker: str, period: str, interval: str) -> str:
    cleaned = "".join(char if char.isalnum() else "_" for char in ticker)
    return f"{cleaned}_{period}_{interval}.csv"


def cache_path(cache_dir: str | Path, ticker: str, period: str, interval: str) -> Path:
    return Path(cache_dir) / safe_cache_name(ticker, period, interval)


def normalize_history_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()

    clean = frame.copy()
    if "Date" in clean.columns or "Datetime" in clean.columns:
        date_column = "Date" if "Date" in clean.columns else "Datetime"
        clean[date_column] = pd.to_datetime(clean[date_column])
        clean = clean.set_index(date_column)
    clean = clean.sort_index()
    clean.index.name = "Date"
    return clean


def fetch_history_yfinance(ticker: str, period: str, interval: str) -> pd.DataFrame:
    frame = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    return normalize_history_frame(frame)


def fetch_history_from_provider(provider: str, ticker: str, period: str, interval: str) -> pd.DataFrame:
    provider = provider.lower().strip()
    if provider == "yfinance":
        return fetch_history_yfinance(ticker, period, interval)
    raise NotImplementedError(
        f"Market provider '{provider}' is not implemented yet. "
        "The fetch layer is structured so Twelve Data, Alpha Vantage, Polygon, or another provider can be added here."
    )


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


def fetch_history_with_retry_and_cache(
    ticker: str,
    period: str,
    interval: str,
    provider: str,
    cache_dir: str | Path,
    retry_count: int,
    retry_backoff_sec: float,
    cache_max_age_hours: int,
) -> tuple[pd.DataFrame, str | None, bool, str | None]:
    errors: list[str] = []
    for attempt in range(1, retry_count + 1):
        try:
            frame = fetch_history_from_provider(provider, ticker, period, interval)
            if frame is None or frame.empty:
                raise ValueError("empty price history")
            save_cache(frame, cache_path(cache_dir, ticker, period, interval))
            return frame, None, False, None
        except Exception as exc:  # noqa: BLE001 - retry and fall back to cache.
            errors.append(f"attempt {attempt}/{retry_count}: {exc}")
            if attempt < retry_count:
                time.sleep(retry_backoff_sec * attempt)

    cached_frame, cache_error = load_cache(cache_path(cache_dir, ticker, period, interval), cache_max_age_hours)
    if not cached_frame.empty:
        return cached_frame, "; ".join(errors), True, None

    error_text = "; ".join(errors)
    if cache_error:
        error_text = f"{error_text}; cache fallback failed: {cache_error}"
    else:
        error_text = f"{error_text}; no cache fallback available"
    return pd.DataFrame(), error_text, False, cache_error


def build_market_quality(assets: list[dict[str, Any]], source: str, fetched_at: str, min_success_ratio: float) -> dict[str, Any]:
    total = len(assets)
    usable = [asset for asset in assets if asset.get("last_close") is not None]
    live_success = [asset for asset in usable if not asset.get("from_cache")]
    cache_success = [asset for asset in usable if asset.get("from_cache")]
    failed = total - len(usable)
    success_ratio = len(usable) / total if total else 0.0
    live_success_ratio = len(live_success) / total if total else 0.0

    warnings: list[str] = []
    if success_ratio < min_success_ratio or live_success_ratio < min_success_ratio:
        warnings.append("行情数据不完整，请谨慎使用")
    if live_success_ratio < min_success_ratio:
        if cache_success:
            warnings.append("实时行情抓取不足，部分数据来自本地缓存，请谨慎使用")
        else:
            warnings.append("当日实时行情抓取不足，请谨慎使用")
    if total and len(live_success) == 0:
        warnings.append("当日实时行情抓取失败，报告不应视为完整行情复盘")

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
        "warnings": warnings,
    }


def fetch_market_data(config: dict[str, Any]) -> dict[str, Any]:
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    market = config.get("market", {})
    period = market.get("period", "90d")
    interval = market.get("interval", "1d")
    provider = market.get("provider", "yfinance")
    source = data_source_for_provider(provider)
    request_delay_sec = float(market.get("request_delay_sec", 2.0))
    retry_count = int(market.get("retry_count", 3))
    retry_backoff_sec = float(market.get("retry_backoff_sec", 10.0))
    cache_dir = market.get("cache_dir", "data/processed/market_cache")
    cache_max_age_hours = int(market.get("cache_max_age_hours", 168))
    min_success_ratio = float(market.get("min_success_ratio", 0.7))
    fetched_at = now_iso(timezone_name)

    assets: list[dict[str, Any]] = []
    universe = universe_from_config(config)
    for index, asset in enumerate(universe):
        if index > 0 and request_delay_sec > 0:
            time.sleep(request_delay_sec)

        frame, error, from_cache, cache_error = fetch_history_with_retry_and_cache(
            asset["ticker"],
            period,
            interval,
            provider,
            cache_dir,
            retry_count,
            retry_backoff_sec,
            cache_max_age_hours,
        )
        summary = summarize_price_frame(frame)
        source_info = {
            "provider": source,
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
            "provider": provider,
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
