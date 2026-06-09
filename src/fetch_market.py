from __future__ import annotations

import argparse
import copy
import json
import os
import re
import signal
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
from dotenv import load_dotenv

from .indicators import summarize_price_frame
from .market_data_provider import MarketProviderError, make_provider, provider_display_name, provider_is_enabled, quote_to_frame as provider_quote_to_frame


DEFAULT_PROVIDER_ORDER = ["fmp", "finnhub", "twelve_data", "yfinance", "cache"]
MARKET_FAILURE_MESSAGE = "今日行情数据抓取失败或不足，未生成正式美股复盘，请检查数据源。"
MARKET_TIMEOUT_MESSAGE = "us-market-review 运行超时，未生成正式美股复盘，请检查数据源。"
DATA_SOURCE_UPGRADE_MESSAGE = "免费行情源无法满足完整报告，需要接入/升级正式数据源。"
RETRYABLE_FAILURE_REASONS = {"rate_limited"}
DEFAULT_CRITICAL_GROUPS = {
    "index_etf": {"tickers": ["SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "VIX"], "min_success_ratio": 1.0},
    "mega_tech": {"tickers": ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA"], "min_success_ratio": 1.0},
    "ai_semis": {"tickers": ["AMD", "AVGO", "MU", "MRVL", "ARM"], "min_success_ratio": 0.8},
}
T = TypeVar("T")


class ProviderCallTimeout(TimeoutError):
    pass


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


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def normalize_provider_name(name: str) -> str:
    normalized = str(name).strip().lower()
    aliases = {
        "financial_modeling_prep": "fmp",
        "financialmodelingprep": "fmp",
        "finn_hub": "finnhub",
        "twelvedata": "twelve_data",
        "twelve": "twelve_data",
        "local_cache": "cache",
        "local market cache": "cache",
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


def configured_provider_order(market: dict[str, Any]) -> list[str]:
    return parse_provider_order(market.get("market_data_provider_order", market.get("provider_order", market.get("provider_chain", DEFAULT_PROVIDER_ORDER))))


def provider_options_for(provider_options: dict[str, Any], provider_name: str) -> dict[str, Any]:
    value = provider_options.get(provider_name) or provider_options.get(provider_name.lower()) or {}
    return value if isinstance(value, dict) else {}


def provider_option_enabled(provider_name: str, options: dict[str, Any]) -> bool:
    if options.get("enabled") is False:
        return False
    if provider_name == "stooq" and not (options.get("enabled") is True or env_bool("ENABLE_STOOQ", False)):
        return False
    return True


def effective_provider_order(provider_order: list[str], provider_options: dict[str, Any]) -> list[str]:
    effective: list[str] = []
    for provider_name in provider_order:
        normalized = normalize_provider_name(provider_name)
        options = provider_options_for(provider_options, normalized)
        if normalized in effective:
            continue
        if normalized != "cache" and (not provider_option_enabled(normalized, options) or not provider_is_enabled(normalized, options)):
            continue
        effective.append(normalized)
    if "cache" not in effective:
        effective.append("cache")
    return effective


def clamp_provider_timeouts(provider_options: dict[str, Any], provider_request_timeout_sec: int) -> dict[str, Any]:
    options = copy.deepcopy(provider_options) if isinstance(provider_options, dict) else {}
    for provider_name in ["fmp", "finnhub", "twelve_data", "stooq"]:
        provider_config = options.setdefault(provider_name, {})
        if isinstance(provider_config, dict):
            configured = int(provider_config.get("timeout") or provider_request_timeout_sec)
            provider_config["timeout"] = max(1, min(configured, provider_request_timeout_sec))
    return options


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
    config_order = configured_provider_order(market)
    env_provider_order = os.getenv("MARKET_DATA_PROVIDER_ORDER")
    if env_provider_order:
        env_order = parse_provider_order(env_provider_order)
        missing_formal = any(provider in config_order and provider not in env_order for provider in ["fmp", "finnhub", "twelve_data"])
        market["market_data_provider_order"] = config_order if missing_formal else env_order
    else:
        market["market_data_provider_order"] = config_order
    market["provider_chain"] = market["market_data_provider_order"]

    env_overrides = {
        "MARKET_REQUEST_DELAY_SEC": ("request_delay_sec", env_float),
        "MARKET_RETRY_COUNT": ("retry_count", env_int),
        "MARKET_RETRY_BACKOFF_SEC": ("retry_backoff_sec", env_float),
        "MARKET_PROVIDER_REQUEST_TIMEOUT_SEC": ("provider_request_timeout_sec", env_int),
        "MARKET_PER_TICKER_TIMEOUT_SEC": ("per_ticker_timeout_sec", env_int),
        "MARKET_FETCH_TIMEOUT_SEC": ("market_fetch_timeout_sec", env_int),
        "TWELVE_DATA_BATCH_SIZE": ("twelve_data_batch_size", env_int),
        "TWELVE_DATA_BATCH_SLEEP_SEC": ("twelve_data_batch_sleep_sec", env_int),
        "TWELVE_DATA_RETRY_SLEEP_SEC": ("twelve_data_retry_sleep_sec", env_int),
        "MARKET_CACHE_MAX_AGE_HOURS": ("cache_max_age_hours", env_int),
        "MARKET_CACHE_MAX_TRADING_DAYS": ("cache_max_trading_days", env_int),
        "MARKET_MIN_SUCCESS_RATIO": ("min_success_ratio", env_float),
        "MARKET_LIVE_MIN_SUCCESS_RATIO": ("live_min_success_ratio", env_float),
        "MARKET_CACHE_MAX_SUCCESS_RATIO": ("cache_max_success_ratio", env_float),
    }
    for env_name, (key, parser) in env_overrides.items():
        if os.getenv(env_name):
            market[key] = parser(env_name, market.get(key))
    if os.getenv("MARKET_CACHE_DIR"):
        market["cache_dir"] = os.getenv("MARKET_CACHE_DIR")
    if os.getenv("MARKET_CACHE_SNAPSHOT_PATH"):
        market["cache_snapshot_path"] = os.getenv("MARKET_CACHE_SNAPSHOT_PATH")
    if os.getenv("MARKET_LATEST_DATA_PATH"):
        market["latest_data_path"] = os.getenv("MARKET_LATEST_DATA_PATH")
    if os.getenv("MARKET_RUN_STATE_PATH"):
        market["run_state_path"] = os.getenv("MARKET_RUN_STATE_PATH")
    if os.getenv("ENABLE_PDF"):
        config.setdefault("report", {}).setdefault("pdf", {})
        config["report"]["pdf"]["enabled"] = os.getenv("ENABLE_PDF", "true").lower() == "true"
    return config


def now_iso(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).replace(microsecond=0).isoformat()


def universe_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    market = config.get("market", {})
    groups = [("benchmarks", "index"), ("sector_etfs", "sector_etf"), ("key_stocks", "key_stock"), ("macro_assets", "macro_asset")]
    seen: set[str] = set()
    universe: list[dict[str, Any]] = []
    for key, category in groups:
        for item in market.get(key, []):
            ticker = str(item.get("ticker", "")).strip().upper()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            universe.append({"ticker": ticker, "name": str(item.get("name") or ticker), "category": category, "theme": str(item.get("theme") or ""), "required": bool(item.get("required", True))})
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
    except Exception as exc:  # noqa: BLE001
        return pd.DataFrame(), f"cache read failed: {exc}"
    if frame.empty:
        return pd.DataFrame(), "cache is empty"
    latest_date = pd.Timestamp(frame.index.max()).date()
    days_since = trading_days_since(latest_date)
    if days_since > max_trading_days:
        return pd.DataFrame(), f"cache latest date {latest_date} is older than {max_trading_days} trading days"
    return frame, None


def merge_cached_history_with_quote(cached_frame: pd.DataFrame, quote_frame: pd.DataFrame) -> pd.DataFrame:
    frames = [frame for frame in [cached_frame, quote_frame] if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames).sort_index()
    merged = merged[~merged.index.duplicated(keep="last")]
    merged.attrs.update(getattr(quote_frame, "attrs", {}))
    merged.attrs["quote_success"] = True
    merged.attrs["quote_only"] = False
    merged.attrs["historical_from_cache"] = True
    return merged


def quote_frame_for_provider(provider: Any, provider_name: str, quote: Any) -> pd.DataFrame:
    quote_to_frame = getattr(provider, "quote_to_frame", None)
    if callable(quote_to_frame):
        return quote_to_frame(quote)
    return provider_quote_to_frame(quote, provider_name, quote_only=True)


def call_with_timeout(func: Callable[[], T], timeout_sec: float, label: str) -> T:
    if timeout_sec <= 0 or not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return func()
    old_handler = signal.getsignal(signal.SIGALRM)

    def handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise ProviderCallTimeout(f"{label} exceeded {timeout_sec:.1f}s")

    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        return func()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def seconds_left(deadline: float | None) -> float:
    if deadline is None:
        return 999999.0
    return max(0.0, deadline - time.monotonic())


def error_category(exc: Exception) -> str:
    if isinstance(exc, MarketProviderError):
        return exc.category
    if isinstance(exc, ProviderCallTimeout):
        return "provider_timeout"
    text = str(exc).lower()
    if "http 402" in text or "payment_required" in text:
        return "payment_required"
    if "http 429" in text or "too many" in text or "rate limit" in text:
        return "rate_limited"
    if "http 403" in text or "permission" in text or "forbidden" in text:
        return "provider_permission_denied"
    if "timeout" in text:
        return "provider_timeout"
    if "no data" in text or "not supported" in text or "not found" in text or "invalid symbol" in text:
        return "symbol_not_supported"
    if "empty" in text:
        return "empty_response"
    if "schema" in text or "parse" in text or "missing price" in text:
        return "schema_parse_error"
    return "quote_failed"


def should_retry(category: str) -> bool:
    return category in RETRYABLE_FAILURE_REASONS


def retry_after_from_text(text: str) -> float | None:
    match = re.search(r"retry[_ -]?after[=: ]+(\d+)", text, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def write_run_state(state: dict[str, Any], path: str | Path) -> None:
    run_state_path = Path(path)
    run_state_path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    run_state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def throttle_twelve_data(throttle_state: dict[str, Any] | None, batch_size: int, batch_sleep_sec: int, deadline: float | None) -> None:
    if not throttle_state or batch_size <= 0 or batch_sleep_sec <= 0:
        return
    count = int(throttle_state.get("request_count") or 0)
    slept_after = int(throttle_state.get("slept_after") or 0)
    if count > 0 and count % batch_size == 0 and slept_after != count:
        sleep_seconds = min(float(batch_sleep_sec), seconds_left(deadline))
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        throttle_state["slept_after"] = count


def provider_chain_source(provider_chain: list[str]) -> str:
    names: list[str] = []
    for provider in provider_chain:
        try:
            names.append(provider_display_name(provider))
        except Exception:  # noqa: BLE001
            names.append(provider)
    return " -> ".join(names)


def provider_order_for_asset(asset: dict[str, Any], market: dict[str, Any], configured_order: list[str], provider_options: dict[str, Any]) -> list[str]:
    routing = market.get("provider_routing", {}) if isinstance(market.get("provider_routing"), dict) else {}
    ticker = str(asset.get("ticker", "")).upper()
    route = routing.get(ticker) or routing.get(str(asset.get("category", ""))) or routing.get("default") or configured_order
    return effective_provider_order(parse_provider_order(route), provider_options)


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
    provider_instances: dict[str, Any] | None = None,
    per_ticker_timeout_sec: int = 60,
    provider_request_timeout_sec: int = 12,
    market_deadline: float | None = None,
    progress_state: dict[str, Any] | None = None,
    run_state_path: str | Path | None = None,
    prefer_cache: bool = False,
    twelve_data_throttle_state: dict[str, Any] | None = None,
    twelve_data_batch_size: int = 3,
    twelve_data_batch_sleep_sec: int = 90,
    twelve_data_retry_sleep_sec: int = 90,
) -> tuple[pd.DataFrame, str | None, bool, str | None, str]:
    errors: list[str] = []
    ticker_cache_path = cache_path(cache_dir, ticker, period, interval)
    provider_instances = provider_instances or {}
    ticker_deadline = min(time.monotonic() + per_ticker_timeout_sec, market_deadline or time.monotonic() + per_ticker_timeout_sec)
    current_provider = ""

    cache_error: str | None = None
    if prefer_cache:
        cached_frame, cache_error = load_cache(ticker_cache_path, cache_max_age_hours, cache_max_trading_days)
        if not cached_frame.empty:
            return cached_frame, None, True, None, "Local market cache"

    for provider_name in provider_order:
        if seconds_left(ticker_deadline) <= 0:
            errors.append("ticker_timeout: full provider chain exceeded per-ticker budget")
            break
        normalized = normalize_provider_name(provider_name)
        current_provider = provider_display_name(normalized) if normalized != "cache" else "Local market cache"
        if progress_state is not None:
            progress_state["current_ticker"] = ticker
            progress_state["current_provider"] = current_provider
            progress_state["last_error"] = ""
            if run_state_path:
                write_run_state(progress_state, run_state_path)

        if normalized == "cache":
            cached_frame, current_cache_error = load_cache(ticker_cache_path, cache_max_age_hours, cache_max_trading_days)
            if not cached_frame.empty:
                return cached_frame, "; ".join(errors) if errors else None, True, None, "Local market cache"
            errors.append(f"cache: {current_cache_error or cache_error or 'no cache fallback available'}")
            continue

        options = provider_options_for(provider_options, normalized)
        if not provider_option_enabled(normalized, options) or not provider_is_enabled(normalized, options):
            continue

        try:
            provider = provider_instances.get(normalized) or make_provider(normalized, options)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{normalized}: {exc}")
            continue

        attempt = 1
        while attempt <= retry_count and seconds_left(ticker_deadline) > 0 and seconds_left(market_deadline) > 0:
            if normalized == "twelve_data":
                throttle_twelve_data(twelve_data_throttle_state, twelve_data_batch_size, twelve_data_batch_sleep_sec, market_deadline)
                if twelve_data_throttle_state is not None:
                    twelve_data_throttle_state["request_count"] = int(twelve_data_throttle_state.get("request_count") or 0) + 1
            request_timeout = min(float(provider_request_timeout_sec), seconds_left(ticker_deadline), seconds_left(market_deadline))
            try:
                fetch_quote = getattr(provider, "fetch_quote", None)
                if callable(fetch_quote):
                    quote = call_with_timeout(lambda: fetch_quote(ticker), request_timeout, f"{provider.name} quote {ticker}")
                    quote_frame = quote_frame_for_provider(provider, normalized, quote)
                    cached_frame, _ = load_cache(ticker_cache_path, cache_max_age_hours, cache_max_trading_days)
                    if not cached_frame.empty and len(cached_frame.dropna(subset=["Close"])) >= 20:
                        frame = merge_cached_history_with_quote(cached_frame, quote_frame)
                        save_cache(frame, ticker_cache_path)
                        return frame, None, False, None, provider.name
                frame = call_with_timeout(lambda: provider.fetch_history(ticker, period, interval), request_timeout, f"{provider.name} history {ticker}")
                if frame is None or frame.empty:
                    raise ValueError("empty price history")
                save_cache(frame, ticker_cache_path)
                return frame, None, False, None, provider.name
            except Exception as exc:  # noqa: BLE001
                category = error_category(exc)
                errors.append(f"{provider.name} {category}: {exc}")
                if progress_state is not None:
                    progress_state["last_error"] = f"{ticker} {provider.name} {category}"
                    if run_state_path:
                        write_run_state(progress_state, run_state_path)
                if not should_retry(category) or attempt >= retry_count:
                    break
                retry_after = retry_after_from_text(str(exc))
                base_sleep = retry_backoff_sec * attempt
                if normalized == "twelve_data":
                    base_sleep = max(float(twelve_data_retry_sleep_sec), retry_after or 0, base_sleep)
                elif retry_after:
                    base_sleep = max(base_sleep, retry_after)
                sleep_seconds = min(base_sleep, seconds_left(ticker_deadline), seconds_left(market_deadline))
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                attempt += 1

    if seconds_left(ticker_deadline) <= 0:
        errors.append("ticker_timeout: current provider chain exceeded budget")
    return pd.DataFrame(), "; ".join(errors), False, None, current_provider


def asset_is_usable(asset: dict[str, Any] | None) -> bool:
    return bool(asset and asset.get("last_close") is not None and (asset.get("daily_change") is not None or asset.get("quote_success")))


def normalize_failure_reason(category: str | None, text: str | None = None) -> str:
    combined = f"{category or ''} {text or ''}".lower()
    if "missing_api_key" in combined:
        return "missing_api_key"
    if "invalid_api_key" in combined:
        return "invalid_api_key"
    if "402" in combined or "payment_required" in combined:
        return "payment_required"
    if "403" in combined or "provider_permission" in combined or "permission" in combined or "forbidden" in combined:
        return "provider_permission_denied"
    if "429" in combined or "rate limit" in combined or "too many requests" in combined:
        return "rate_limited"
    if "timeout" in combined and "ticker" in combined:
        return "ticker_timeout"
    if "timeout" in combined:
        return "provider_timeout"
    if "schema" in combined or "parse" in combined or "missing price" in combined:
        return "schema_parse_error"
    if "no data" in combined or "not supported" in combined or "not found" in combined or "invalid symbol" in combined:
        return "symbol_not_supported"
    if "empty" in combined or "no cache fallback available" in combined or "cache is empty" in combined:
        return "empty_response"
    return "quote_failed"


def classify_failure_reason(asset: dict[str, Any]) -> str:
    if asset_is_usable(asset):
        return ""
    source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
    error_text = " ".join(str(value or "") for value in [asset.get("error"), asset.get("cache_error"), source.get("historical_error_category"), source.get("historical_error")])
    return normalize_failure_reason("", error_text)


def failure_detail(asset: dict[str, Any]) -> dict[str, Any]:
    source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
    return {
        "ticker": asset.get("ticker"),
        "reason": asset.get("failure_reason") or classify_failure_reason(asset),
        "quote_success": bool(asset.get("quote_success")),
        "historical_success": bool(asset.get("historical_success")),
        "provider": source.get("provider") or "",
        "provider_symbol": source.get("provider_symbol") or asset.get("ticker"),
        "company_name": source.get("company_name") or "",
    }


def critical_group_results(assets: list[dict[str, Any]], critical_groups: dict[str, Any]) -> dict[str, Any]:
    lookup = {str(asset.get("ticker")): asset for asset in assets}
    results: dict[str, Any] = {}
    for name, group_config in critical_groups.items():
        tickers = [str(ticker) for ticker in group_config.get("tickers", [])]
        min_ratio = float(group_config.get("min_success_ratio", 1.0))
        usable = [ticker for ticker in tickers if asset_is_usable(lookup.get(ticker, {}))]
        failed = [ticker for ticker in tickers if ticker not in usable]
        ratio = len(usable) / len(tickers) if tickers else 1.0
        results[name] = {"tickers": tickers, "success_tickers": usable, "failed_tickers": failed, "success_ratio": ratio, "min_success_ratio": min_ratio, "passed": ratio >= min_ratio}
    return results


def build_market_quality(assets: list[dict[str, Any]], source: str, fetched_at: str, min_success_ratio: float, critical_groups: dict[str, Any] | None = None) -> dict[str, Any]:
    required_assets = [asset for asset in assets if asset.get("required", True)]
    usable = [asset for asset in required_assets if asset_is_usable(asset)]
    failed_assets = [asset for asset in required_assets if not asset_is_usable(asset)]
    live_success = [asset for asset in usable if not asset.get("from_cache")]
    cache_success = [asset for asset in usable if asset.get("from_cache")]
    total = len(required_assets)
    success_ratio = len(usable) / total if total else 0.0
    live_success_ratio = len(live_success) / total if total else 0.0
    cache_success_ratio = len(cache_success) / total if total else 0.0
    provider_counts: dict[str, int] = {}
    for asset in usable:
        provider = str(asset.get("source", {}).get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
    groups = critical_group_results(required_assets, critical_groups or DEFAULT_CRITICAL_GROUPS)
    critical_groups_passed = all(group.get("passed") for group in groups.values())
    failed_details = [failure_detail(asset) for asset in failed_assets]
    quote_only_tickers = [str(asset.get("ticker")) for asset in usable if asset.get("quote_success") and not asset.get("historical_success")]
    formal_report_allowed = total > 0 and success_ratio >= min_success_ratio and critical_groups_passed
    warnings: list[str] = []
    if not formal_report_allowed:
        warnings.extend([MARKET_FAILURE_MESSAGE, DATA_SOURCE_UPGRADE_MESSAGE])
    elif quote_only_tickers:
        warnings.append(f"部分标的历史指标暂缺，但 quote 可用: {', '.join(quote_only_tickers)}。")
    return {
        "source": source,
        "fetched_at": fetched_at,
        "total_count": total,
        "success_count": len(usable),
        "live_success_count": len(live_success),
        "cache_success_count": len(cache_success),
        "failed_count": len(failed_assets),
        "success_ratio": success_ratio,
        "live_success_ratio": live_success_ratio,
        "cache_success_ratio": cache_success_ratio,
        "min_success_ratio": min_success_ratio,
        "data_complete": success_ratio >= min_success_ratio,
        "live_data_complete": live_success_ratio >= min_success_ratio,
        "formal_report_allowed": formal_report_allowed,
        "critical_groups": groups,
        "critical_groups_passed": critical_groups_passed,
        "provider_counts": provider_counts,
        "warnings": warnings,
        "needs_data_source_upgrade": not formal_report_allowed,
        "upgrade_message": DATA_SOURCE_UPGRADE_MESSAGE if not formal_report_allowed else "",
        "success_tickers": [str(asset.get("ticker")) for asset in usable],
        "failed_tickers": [str(item.get("ticker")) for item in failed_details],
        "failed_details": failed_details,
        "quote_success_tickers": [str(asset.get("ticker")) for asset in required_assets if asset.get("quote_success")],
        "historical_success_tickers": [str(asset.get("ticker")) for asset in required_assets if asset.get("historical_success")],
        "quote_only_tickers": quote_only_tickers,
        "extension_failed_details": [],
    }


def save_market_cache_snapshot(market_data: dict[str, Any], snapshot_path: str | Path) -> None:
    metadata = market_data.get("metadata", {})
    if int(metadata.get("success_count") or 0) <= 0:
        return
    path = Path(snapshot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metadata": metadata, "assets": [asset for asset in market_data.get("assets", []) if asset.get("last_close") is not None]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_provider_instances(provider_orders: list[list[str]], provider_options: dict[str, Any], universe: list[dict[str, Any]], provider_request_timeout_sec: int) -> dict[str, Any]:
    providers = []
    for order in provider_orders:
        providers.extend(order)
    instances: dict[str, Any] = {}
    for provider_name in providers:
        normalized = normalize_provider_name(provider_name)
        options = provider_options_for(provider_options, normalized)
        if normalized in instances or normalized == "cache" or not provider_option_enabled(normalized, options) or not provider_is_enabled(normalized, options):
            continue
        try:
            provider = make_provider(normalized, options)
        except Exception:
            continue
        instances[normalized] = provider
        prefetch_quotes = getattr(provider, "prefetch_quotes", None)
        if callable(prefetch_quotes):
            try:
                tickers = [item["ticker"] for item in universe]
                call_with_timeout(lambda: prefetch_quotes(tickers), provider_request_timeout_sec, f"{provider.name} batch quote")
            except Exception:
                pass
    return instances


def failed_asset_record(asset: dict[str, Any], fetched_at: str, source: str, provider: str, error: str, reason: str) -> dict[str, Any]:
    source_info = {"provider": provider or source, "provider_chain": source, "ticker": asset["ticker"], "provider_symbol": asset["ticker"], "period": "", "interval": "", "as_of": None, "fetched_at": fetched_at, "from_cache": False, "quote_success": False, "quote_only": False, "historical_success": False, "historical_from_cache": False, "historical_error_category": reason, "historical_error": ""}
    record = {**asset, **summarize_price_frame(pd.DataFrame()), "source": source_info, "error": error, "from_cache": False, "cache_error": None, "quote_success": False, "quote_only": False, "historical_success": False, "indicator_reason": ""}
    record["failure_reason"] = reason
    return record


def write_market_provider_diagnostics(market_data: dict[str, Any]) -> None:
    metadata = market_data.get("metadata", {})
    assets = market_data.get("assets", [])
    path = Path("logs/provider_check.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"daily_market_run generated_at={datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"source={metadata.get('source', '')}",
        f"formal_report_allowed={metadata.get('formal_report_allowed')}",
        f"full_pool_success={metadata.get('success_count', 0)}/{metadata.get('total_count', 0)} ratio={float(metadata.get('success_ratio') or 0.0) * 100:.1f}%",
        f"live_success={metadata.get('live_success_count', 0)} cache_success={metadata.get('cache_success_count', 0)} failed={metadata.get('failed_count', 0)}",
        f"market_fetch_timed_out={metadata.get('market_fetch_timed_out')}",
        f"current_provider={metadata.get('current_provider', '')}",
        f"current_ticker={metadata.get('current_ticker', '')}",
        f"unfinished_tickers={','.join(metadata.get('unfinished_tickers', []))}",
        f"success_tickers={','.join(metadata.get('success_tickers', []))}",
        f"failed_tickers={','.join(metadata.get('failed_tickers', []))}",
        f"critical_groups={json.dumps(metadata.get('critical_groups', {}), ensure_ascii=False)}",
    ]
    for asset in assets:
        source_info = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
        status = "ok" if asset_is_usable(asset) else "failed"
        reason = asset.get("failure_reason") or asset.get("indicator_reason") or ""
        lines.append(" ".join([f"ticker={asset.get('ticker')}", f"required={'yes' if asset.get('required', True) else 'no'}", f"status={status}", f"quote_success={'yes' if asset.get('quote_success') else 'no'}", f"historical_success={'yes' if asset.get('historical_success') else 'no'}", f"provider={source_info.get('provider') or ''}", f"symbol={source_info.get('provider_symbol') or asset.get('ticker')}", f"company={source_info.get('company_name') or ''}", f"failure_reason={reason}", f"as_of={asset.get('as_of') or ''}"]))
    text = "\n".join(lines) + "\n"
    if path.exists() and path.stat().st_size > 0:
        with path.open("a", encoding="utf-8") as file:
            file.write("\n---\n")
            file.write(text)
    else:
        path.write_text(text, encoding="utf-8")


def fetch_market_data(config: dict[str, Any]) -> dict[str, Any]:
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    market = config.get("market", {})
    period = market.get("period", "90d")
    interval = market.get("interval", "1d")
    configured_order = configured_provider_order(market)
    provider_request_timeout_sec = int(market.get("provider_request_timeout_sec", 12))
    provider_options = clamp_provider_timeouts(market.get("provider_options", {}) if isinstance(market.get("provider_options", {}), dict) else {}, provider_request_timeout_sec)
    request_delay_sec = float(market.get("request_delay_sec", 0.2))
    retry_count = max(1, int(market.get("retry_count", 2)))
    retry_backoff_sec = float(market.get("retry_backoff_sec", 30.0))
    per_ticker_timeout_sec = int(market.get("per_ticker_timeout_sec", 60))
    market_fetch_timeout_sec = int(market.get("market_fetch_timeout_sec", 1200))
    twelve_data_batch_size = int(market.get("twelve_data_batch_size", 3))
    twelve_data_batch_sleep_sec = int(market.get("twelve_data_batch_sleep_sec", 90))
    twelve_data_retry_sleep_sec = int(market.get("twelve_data_retry_sleep_sec", 90))
    cache_dir = market.get("cache_dir", "data/processed/market_cache")
    cache_snapshot_path = market.get("cache_snapshot_path", "data/processed/market_cache.json")
    run_state_path = market.get("run_state_path", "data/processed/market_run_state.json")
    cache_max_age_hours = int(market.get("cache_max_age_hours", 168))
    cache_max_trading_days = int(market.get("cache_max_trading_days", 3))
    min_success_ratio = float(market.get("min_success_ratio", 0.9))
    critical_groups = market.get("critical_groups") if isinstance(market.get("critical_groups"), dict) else DEFAULT_CRITICAL_GROUPS
    prefer_cache = bool(market.get("prefer_cache", False))
    fetched_at = now_iso(timezone_name)
    universe = universe_from_config(config)
    route_orders = [provider_order_for_asset(asset, market, configured_order, provider_options) for asset in universe]
    source = provider_chain_source(effective_provider_order(configured_order, provider_options))
    market_deadline = time.monotonic() + market_fetch_timeout_sec
    progress_state: dict[str, Any] = {"started_at": fetched_at, "current_ticker": "", "current_provider": "", "success_tickers": [], "failed_tickers": [], "pending_tickers": [item["ticker"] for item in universe], "last_error": "", "reason": "running"}
    write_run_state(progress_state, run_state_path)

    assets: list[dict[str, Any]] = []
    timed_out = False
    twelve_data_throttle_state: dict[str, Any] = {"request_count": 0, "slept_after": 0}
    provider_instances = build_provider_instances(route_orders, provider_options, universe, provider_request_timeout_sec)
    for index, asset in enumerate(universe):
        if seconds_left(market_deadline) <= 0:
            timed_out = True
            break
        if index > 0 and request_delay_sec > 0:
            time.sleep(min(request_delay_sec, seconds_left(market_deadline)))
        asset_order = route_orders[index]
        asset_chain = provider_chain_source(asset_order)
        progress_state["current_ticker"] = asset["ticker"]
        progress_state["pending_tickers"] = [item["ticker"] for item in universe[index:]]
        write_run_state(progress_state, run_state_path)
        frame, error, from_cache, cache_error, actual_provider = fetch_history_with_provider_order(
            asset["ticker"], period, interval, asset_order, provider_options, cache_dir, retry_count, retry_backoff_sec,
            cache_max_age_hours, cache_max_trading_days, provider_instances, per_ticker_timeout_sec, provider_request_timeout_sec,
            market_deadline, progress_state, run_state_path, prefer_cache, twelve_data_throttle_state, twelve_data_batch_size,
            twelve_data_batch_sleep_sec, twelve_data_retry_sleep_sec,
        )
        summary = summarize_price_frame(frame)
        frame_attrs = getattr(frame, "attrs", {}) if frame is not None else {}
        quote_success = bool(frame_attrs.get("quote_success") or summary.get("last_close") is not None)
        quote_only = bool(frame_attrs.get("quote_only") or frame_attrs.get("fmp_quote_only"))
        historical_success = bool(summary.get("last_close") is not None and not quote_only and len(frame.dropna(subset=["Close"])) >= 2 if frame is not None and not frame.empty and "Close" in frame.columns else False)
        indicator_reason = "historical_failed" if quote_success and not historical_success else ""
        source_info = {
            "provider": actual_provider or asset_chain,
            "provider_chain": asset_chain,
            "ticker": asset["ticker"],
            "provider_symbol": frame_attrs.get("provider_symbol") or frame_attrs.get("fmp_symbol") or asset["ticker"],
            "period": period,
            "interval": interval,
            "as_of": summary.get("as_of"),
            "fetched_at": fetched_at,
            "from_cache": from_cache,
            "quote_success": quote_success,
            "quote_only": quote_only,
            "historical_success": historical_success,
            "historical_from_cache": bool(frame_attrs.get("historical_from_cache")),
            "historical_error_category": frame_attrs.get("historical_error_category", ""),
            "historical_error": frame_attrs.get("historical_error", ""),
            "cache_path": str(cache_path(cache_dir, asset["ticker"], period, interval)) if from_cache else "",
            "company_name": frame_attrs.get("company_name", ""),
            "exchange": frame_attrs.get("exchange", ""),
            "currency": frame_attrs.get("currency", ""),
        }
        asset_record = {**asset, **summary, "source": source_info, "error": error, "from_cache": from_cache, "cache_error": cache_error, "quote_success": quote_success, "quote_only": quote_only, "historical_success": historical_success, "indicator_reason": indicator_reason}
        asset_record["failure_reason"] = classify_failure_reason(asset_record)
        assets.append(asset_record)
        if asset_is_usable(asset_record):
            progress_state["success_tickers"].append(asset["ticker"])
        else:
            progress_state["failed_tickers"].append(asset["ticker"])
        progress_state["pending_tickers"] = [item["ticker"] for item in universe[index + 1 :]]
        write_run_state(progress_state, run_state_path)

    if timed_out:
        processed = {str(asset.get("ticker")) for asset in assets}
        for asset in universe:
            if asset["ticker"] not in processed:
                assets.append(failed_asset_record(asset, fetched_at, source, progress_state.get("current_provider", ""), MARKET_TIMEOUT_MESSAGE, "market_fetch_timeout"))
        progress_state["reason"] = "market_fetch_timeout"
        write_run_state(progress_state, run_state_path)

    quality = build_market_quality(assets, source, fetched_at, min_success_ratio, critical_groups)
    unfinished_tickers = progress_state.get("pending_tickers", []) if timed_out else []
    if timed_out:
        quality["warnings"] = [MARKET_TIMEOUT_MESSAGE] + [warning for warning in quality.get("warnings", []) if warning != MARKET_TIMEOUT_MESSAGE]
        quality["formal_report_allowed"] = False
        quality["needs_data_source_upgrade"] = True
    market_data = {
        "metadata": {
            "source": source,
            "configured_market_data_provider_order": configured_order,
            "provider_chain": effective_provider_order(configured_order, provider_options),
            "market_data_provider_order": effective_provider_order(configured_order, provider_options),
            "period": period,
            "interval": interval,
            "fetched_at": fetched_at,
            "timezone": timezone_name,
            "request_delay_sec": request_delay_sec,
            "retry_count": retry_count,
            "retry_backoff_sec": retry_backoff_sec,
            "provider_request_timeout_sec": provider_request_timeout_sec,
            "per_ticker_timeout_sec": per_ticker_timeout_sec,
            "market_fetch_timeout_sec": market_fetch_timeout_sec,
            "twelve_data_batch_size": twelve_data_batch_size,
            "twelve_data_batch_sleep_sec": twelve_data_batch_sleep_sec,
            "twelve_data_request_count": twelve_data_throttle_state.get("request_count", 0),
            "market_fetch_timed_out": timed_out,
            "timeout_message": MARKET_TIMEOUT_MESSAGE if timed_out else "",
            "current_provider": progress_state.get("current_provider", ""),
            "current_ticker": progress_state.get("current_ticker", ""),
            "unfinished_tickers": unfinished_tickers,
            "last_error": progress_state.get("last_error", ""),
            "cache_dir": str(cache_dir),
            "cache_snapshot_path": str(cache_snapshot_path),
            "run_state_path": str(run_state_path),
            "cache_max_age_hours": cache_max_age_hours,
            "cache_max_trading_days": cache_max_trading_days,
            "prefer_cache": prefer_cache,
            **quality,
        },
        "assets": assets,
    }
    save_market_cache_snapshot(market_data, cache_snapshot_path)
    write_market_provider_diagnostics(market_data)
    progress_state["reason"] = "finished_timeout" if timed_out else "finished"
    write_run_state(progress_state, run_state_path)
    return market_data


def assets_by_category(market_data: dict[str, Any], category: str) -> list[dict[str, Any]]:
    return [asset for asset in market_data.get("assets", []) if asset.get("category") == category]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch market data and print a JSON snapshot.")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "config.yaml"))
    parser.add_argument("--output", default="")
    parser.add_argument("--prefer-cache", action="store_true", help="Prefer local cache before live providers.")
    args = parser.parse_args()
    config = load_config(args.config)
    if args.prefer_cache:
        config.setdefault("market", {})["prefer_cache"] = True
    market_data = fetch_market_data(config)
    payload = json.dumps(market_data, ensure_ascii=False, indent=2, default=str)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
