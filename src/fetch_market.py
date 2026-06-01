from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
from dotenv import load_dotenv

from .indicators import summarize_price_frame
from .market_data_provider import make_provider, provider_display_name


DEFAULT_PROVIDER_ORDER = ["stooq", "yfinance", "cache"]
MARKET_FAILURE_MESSAGE = "今日行情数据抓取失败或不足，未生成正式美股复盘，请检查数据源。"


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


def normalize_provider_name(name: str) -> str:
    normalized = str(name).strip().lower()
    aliases = {
        "local_cache": "cache",
        "local market cache": "cache",
        "twelvedata": "twelve_data",
        "alphavantage": "alpha_vantage",
    }
    return aliases.get(normalized, normalized)


def parse_provider_order(value: Any) -> list[str]:
    if value is None or value == "":
        providers = DEFAULT_PROVIDER_ORDER.copy()
    elif isinstance(value, list):
        providers = [normalize_provider_name(str(item)) for item in value if str(item).strip()]
    else:
        providers = [normalize_provider_name(item) for item in str(value).split(",") if item.strip()]

    providers = providers or DEFAULT_PROVIDER_ORDER.copy()
    if "cache" not in providers:
        providers.append("cache")
    return providers


# Backward-compatible name used by health_check and older callers.
def parse_provider_chain(value: Any) -> list[str]:
    return parse_provider_order(value)


def configured_provider_order(market: dict[str, Any]) -> list[str]:
    return parse_provider_order(
        market.get(
            "market_data_provider_order",
            market.get("provider_order", market.get("provider_chain", DEFAULT_PROVIDER_ORDER)),
        )
    )


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
    # Only the new env name may override provider order. Old MARKET_PROVIDER/MARKET_PROVIDER_CHAIN
    # values are ignored so an existing server .env cannot accidentally disable Stooq fallback.
    if os.getenv("MARKET_DATA_PROVIDER_ORDER"):
        market["market_data_provider_order"] = parse_provider_order(os.getenv("MARKET_DATA_PROVIDER_ORDER"))
    else:
        market["market_data_provider_order"] = configured_provider_order(market)
    market["provider_chain"] = market["market_data_provider_order"]

    if os.getenv("MARKET_REQUEST_DELAY_SEC"):
        market["request_delay_sec"] = env_float("MARKET_REQUEST_DELAY_SEC", float(market.get("request_delay_sec", 1.0)))
    if os.getenv("MARKET_RETRY_COUNT"):
        market["retry_count"] = env_int("MARKET_RETRY_COUNT", int(market.get("retry_count", 1)))
    if os.getenv("MARKET_RETRY_BACKOFF_SEC"):
        market["retry_backoff_sec"] = env_float("MARKET_RETRY_BACKOFF_SEC", float(market.get("retry_backoff_sec", 4.0)))
    if os.getenv("MARKET_CACHE_DIR"):
        market["cache_dir"] = os.getenv("MARKET_CACHE_DIR")
    if os.getenv("MARKET_CACHE_SNAPSHOT_PATH"):
        market["cache_snapshot_path"] = os.getenv("MARKET_CACHE_SNAPSHOT_PATH")
    if os.getenv("MARKET_CACHE_MAX_AGE_HOURS"):
        market["cache_max_age_hours"] = env_int("MARKET_CACHE_MAX_AGE_HOURS", int(market.get("cache_max_age_hours", 168)))
    if os.getenv("MARKET_CACHE_MAX_TRADING_DAYS"):
        market["cache_max_trading_days"] = env_int("MARKET_CACHE_MAX_TRADING_DAYS", int(market.get("cache_max_trading_days", 3)))
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


def trading_days_since(latest: date, today: date | None = None) -> int:
    today = today or datetime.now().date()
    if latest >= today:
        return 0
    days = 0
    cursor = latest + timedelta(days=1)
    while cursor <= today:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def load_cache(path: Path, max_age_hours: int, max_trading_days: int) -> tuple[pd.DataFrame, str | None]:
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

    latest_index = pd.Timestamp(frame.index.max())
    latest_date = latest_index.date()
    days_since = trading_days_since(latest_date)
    if days_since > max_trading_days:
        return pd.DataFrame(), f"cache latest date {latest_date} is older than {max_trading_days} trading days"

    return frame, None


def provider_options_for(provider_options: dict[str, Any], provider_name: str) -> dict[str, Any]:
    value = provider_options.get(provider_name) or provider_options.get(provider_name.lower()) or {}
    return value if isinstance(value, dict) else {}


def fetch_history_with_provider_order(
    ticker: str,
    period: str,
    interval: str,
    provider_order: list[str],
    provider_options: dict[str, Any],
    cache_dir: str | Path,
    retry_count: int,
    retry_backoff_sec: float,
    cache_max_age_hours: int,
    cache_max_trading_days: int,
) -> tuple[pd.DataFrame, str | None, bool, str | None, str]:
    errors: list[str] = []
    ticker_cache_path = cache_path(cache_dir, ticker, period, interval)

    for provider_name in provider_order:
        normalized = normalize_provider_name(provider_name)
        if normalized == "cache":
            cached_frame, cache_error = load_cache(ticker_cache_path, cache_max_age_hours, cache_max_trading_days)
            if not cached_frame.empty:
                return cached_frame, "; ".join(errors) if errors else None, True, None, "Local market cache"
            if cache_error:
                errors.append(f"cache: {cache_error}")
            else:
                errors.append("cache: no cache fallback available")
            continue

        try:
            provider = make_provider(normalized, provider_options_for(provider_options, normalized))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{normalized}: {exc}")
            continue

        for attempt in range(1, retry_count + 1):
            try:
                frame = provider.fetch_history(ticker, period, interval)
                if frame is None or frame.empty:
                    raise ValueError("empty price history")
                save_cache(frame, ticker_cache_path)
                return frame, None, False, None, provider.name
            except Exception as exc:  # noqa: BLE001 - try next provider, then cache.
                errors.append(f"{provider.name} attempt {attempt}/{retry_count}: {exc}")
                if attempt < retry_count:
                    time.sleep(retry_backoff_sec * attempt)

    return pd.DataFrame(), "; ".join(errors), False, None, ""


# Backward-compatible wrapper used by older imports.
def fetch_history_with_provider_chain(
    ticker: str,
    period: str,
    interval: str,
    provider_chain: list[str],
    provider_options: dict[str, Any],
    cache_dir: str | Path,
    retry_count: int,
    retry_backoff_sec: float,
    cache_max_age_hours: int,
) -> tuple[pd.DataFrame, str | None, bool, str | None, str]:
    return fetch_history_with_provider_order(
        ticker,
        period,
        interval,
        parse_provider_order(provider_chain),
        provider_options,
        cache_dir,
        retry_count,
        retry_backoff_sec,
        cache_max_age_hours,
        3,
    )


def build_market_quality(assets: list[dict[str, Any]], source: str, fetched_at: str, min_success_ratio: float) -> dict[str, Any]:
    total = len(assets)
    usable = [asset for asset in assets if asset.get("last_close") is not None and asset.get("daily_change") is not None]
    live_success = [asset for asset in usable if not asset.get("from_cache")]
    cache_success = [asset for asset in usable if asset.get("from_cache")]
    failed = total - len(usable)
    success_ratio = len(usable) / total if total else 0.0
    live_success_ratio = len(live_success) / total if total else 0.0
    cache_success_ratio = len(cache_success) / total if total else 0.0
    provider_counts: dict[str, int] = {}
    for asset in usable:
        provider = str(asset.get("source", {}).get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1

    warnings: list[str] = []
    formal_report_allowed = total > 0 and success_ratio >= min_success_ratio
    if not formal_report_allowed:
        warnings.append(MARKET_FAILURE_MESSAGE)
    elif cache_success:
        warnings.append(f"部分行情使用本地缓存降级: {len(cache_success)} 项，请结合获取时间判断时效性。")
    elif failed:
        warnings.append(f"行情数据存在缺口: 失败 {failed} 项，但可用率仍达到正式报告阈值。")

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
        "cache_success_ratio": cache_success_ratio,
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


def save_market_cache_snapshot(market_data: dict[str, Any], snapshot_path: str | Path) -> None:
    metadata = market_data.get("metadata", {})
    if int(metadata.get("success_count") or 0) <= 0:
        return
    path = Path(snapshot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": metadata,
        "assets": [asset for asset in market_data.get("assets", []) if asset.get("last_close") is not None],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def fetch_market_data(config: dict[str, Any]) -> dict[str, Any]:
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    market = config.get("market", {})
    period = market.get("period", "90d")
    interval = market.get("interval", "1d")
    provider_order = configured_provider_order(market)
    provider_options = market.get("provider_options", {}) if isinstance(market.get("provider_options", {}), dict) else {}
    source = provider_chain_source(provider_order)
    request_delay_sec = float(market.get("request_delay_sec", 1.0))
    retry_count = max(1, int(market.get("retry_count", 1)))
    retry_backoff_sec = float(market.get("retry_backoff_sec", 4.0))
    cache_dir = market.get("cache_dir", "data/processed/market_cache")
    cache_snapshot_path = market.get("cache_snapshot_path", "data/processed/market_cache.json")
    cache_max_age_hours = int(market.get("cache_max_age_hours", 168))
    cache_max_trading_days = int(market.get("cache_max_trading_days", 3))
    min_success_ratio = float(market.get("min_success_ratio", 0.7))
    fetched_at = now_iso(timezone_name)

    assets: list[dict[str, Any]] = []
    universe = universe_from_config(config)
    for index, asset in enumerate(universe):
        if index > 0 and request_delay_sec > 0:
            time.sleep(request_delay_sec)

        frame, error, from_cache, cache_error, actual_provider = fetch_history_with_provider_order(
            asset["ticker"],
            period,
            interval,
            provider_order,
            provider_options,
            cache_dir,
            retry_count,
            retry_backoff_sec,
            cache_max_age_hours,
            cache_max_trading_days,
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
    market_data = {
        "metadata": {
            "source": source,
            "provider_chain": provider_order,
            "market_data_provider_order": provider_order,
            "period": period,
            "interval": interval,
            "fetched_at": fetched_at,
            "timezone": timezone_name,
            "request_delay_sec": request_delay_sec,
            "retry_count": retry_count,
            "retry_backoff_sec": retry_backoff_sec,
            "cache_dir": str(cache_dir),
            "cache_snapshot_path": str(cache_snapshot_path),
            "cache_max_age_hours": cache_max_age_hours,
            "cache_max_trading_days": cache_max_trading_days,
            **quality,
        },
        "assets": assets,
    }
    save_market_cache_snapshot(market_data, cache_snapshot_path)
    return market_data


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
