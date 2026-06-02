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
from .market_data_provider import make_provider, provider_display_name, provider_is_enabled


DEFAULT_PROVIDER_ORDER = ["fmp", "stooq", "yfinance", "cache"]
MARKET_FAILURE_MESSAGE = "今日行情数据抓取失败或不足，未生成正式美股复盘，请检查数据源。"
ALLOWED_FAILURE_REASONS = {
    "quote_failed",
    "historical_failed",
    "symbol_not_supported",
    "permission_denied",
    "rate_limited",
    "schema_parse_error",
    "empty_response",
}


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
        "financial_modeling_prep": "fmp",
        "financialmodelingprep": "fmp",
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


def provider_options_for(provider_options: dict[str, Any], provider_name: str) -> dict[str, Any]:
    value = provider_options.get(provider_name) or provider_options.get(provider_name.lower()) or {}
    return value if isinstance(value, dict) else {}


def effective_provider_order(provider_order: list[str], provider_options: dict[str, Any]) -> list[str]:
    effective: list[str] = []
    for provider_name in provider_order:
        normalized = normalize_provider_name(provider_name)
        if normalized in effective:
            continue
        if not provider_is_enabled(normalized, provider_options_for(provider_options, normalized)):
            continue
        effective.append(normalized)

    if "cache" not in effective:
        effective.append("cache")
    return effective


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
    config_provider_order = configured_provider_order(market)
    env_provider_order = os.getenv("MARKET_DATA_PROVIDER_ORDER")
    # Only the new env name may override provider order. A legacy server .env that still says
    # stooq,yfinance,cache should not silently block the new FMP-first config.yaml default.
    if env_provider_order:
        env_order = parse_provider_order(env_provider_order)
        if "fmp" in config_provider_order and "fmp" not in env_order:
            market["market_data_provider_order"] = config_provider_order
        else:
            market["market_data_provider_order"] = env_order
    else:
        market["market_data_provider_order"] = config_provider_order
    market["provider_chain"] = market["market_data_provider_order"]

    if os.getenv("MARKET_REQUEST_DELAY_SEC"):
        market["request_delay_sec"] = env_float("MARKET_REQUEST_DELAY_SEC", float(market.get("request_delay_sec", 0.3)))
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


def universe_from_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    market = config.get("market", {})
    groups = [
        ("benchmarks", "index"),
        ("sector_etfs", "sector_etf"),
        ("key_stocks", "key_stock"),
        ("macro_assets", "macro_asset"),
    ]

    seen: set[str] = set()
    universe: list[dict[str, Any]] = []
    for key, category in groups:
        for item in market.get(key, []):
            ticker = str(item.get("ticker", "")).strip()
            if not ticker or ticker in seen:
                continue
            seen.add(ticker)
            required = item.get("required")
            if required is None:
                required = category != "macro_asset"
            universe.append(
                {
                    "ticker": ticker,
                    "name": str(item.get("name") or ticker),
                    "category": category,
                    "theme": str(item.get("theme") or ""),
                    "required": bool(required),
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
) -> tuple[pd.DataFrame, str | None, bool, str | None, str]:
    errors: list[str] = []
    ticker_cache_path = cache_path(cache_dir, ticker, period, interval)
    provider_instances = provider_instances or {}

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

        if not provider_is_enabled(normalized, provider_options_for(provider_options, normalized)):
            continue

        try:
            provider = provider_instances.get(normalized)
            if provider is None:
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


def asset_is_usable(asset: dict[str, Any]) -> bool:
    if asset.get("last_close") is None:
        return False
    if asset.get("daily_change") is not None:
        return True
    return bool(asset.get("quote_success"))


def normalize_failure_reason(category: str | None, detail: str | None = None) -> str:
    category_text = str(category or "").strip().lower()
    detail_text = str(detail or "").strip().lower()
    combined = f"{category_text} {detail_text}"
    if category_text in ALLOWED_FAILURE_REASONS:
        return category_text
    if "429" in combined or "rate limit" in combined or "rate_limited" in combined or "too many requests" in combined:
        return "rate_limited"
    if "403" in combined or "permission" in combined or "forbidden" in combined or "permission_denied" in combined:
        return "permission_denied"
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
    error_text = " ".join(
        str(value or "")
        for value in [
            asset.get("error"),
            asset.get("cache_error"),
            source.get("historical_error_category"),
            source.get("historical_error"),
        ]
    )
    return normalize_failure_reason("", error_text)


def failure_detail(asset: dict[str, Any]) -> dict[str, Any]:
    source = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
    reason = asset.get("failure_reason") or classify_failure_reason(asset)
    return {
        "ticker": asset.get("ticker"),
        "reason": reason,
        "quote_success": bool(asset.get("quote_success")),
        "historical_success": bool(asset.get("historical_success")),
        "provider": source.get("provider") or "",
        "provider_symbol": source.get("provider_symbol") or asset.get("ticker"),
    }


def build_market_quality(assets: list[dict[str, Any]], source: str, fetched_at: str, min_success_ratio: float) -> dict[str, Any]:
    core_assets = [asset for asset in assets if asset.get("required", True)]
    extension_assets = [asset for asset in assets if not asset.get("required", True)]

    core_usable = [asset for asset in core_assets if asset_is_usable(asset)]
    core_failed_assets = [asset for asset in core_assets if not asset_is_usable(asset)]
    core_live_success = [asset for asset in core_usable if not asset.get("from_cache")]
    core_cache_success = [asset for asset in core_usable if asset.get("from_cache")]

    extension_usable = [asset for asset in extension_assets if asset_is_usable(asset)]
    extension_failed_assets = [asset for asset in extension_assets if not asset_is_usable(asset)]

    total = len(core_assets)
    failed = len(core_failed_assets)
    success_ratio = len(core_usable) / total if total else 0.0
    live_success_ratio = len(core_live_success) / total if total else 0.0
    cache_success_ratio = len(core_cache_success) / total if total else 0.0

    provider_counts: dict[str, int] = {}
    for asset in core_usable + extension_usable:
        provider = str(asset.get("source", {}).get("provider") or "unknown")
        provider_counts[provider] = provider_counts.get(provider, 0) + 1

    success_tickers = [str(asset.get("ticker")) for asset in core_usable]
    failed_details = [failure_detail(asset) for asset in core_failed_assets]
    failed_tickers = [str(item.get("ticker")) for item in failed_details]
    quote_success_tickers = [str(asset.get("ticker")) for asset in core_assets if asset.get("quote_success")]
    historical_success_tickers = [str(asset.get("ticker")) for asset in core_assets if asset.get("historical_success")]
    quote_only_tickers = [
        str(asset.get("ticker"))
        for asset in core_usable
        if asset.get("quote_success") and not asset.get("historical_success")
    ]
    extension_failed_details = [failure_detail(asset) for asset in extension_failed_assets]

    warnings: list[str] = []
    formal_report_allowed = total > 0 and success_ratio >= min_success_ratio
    if not formal_report_allowed:
        warnings.append(MARKET_FAILURE_MESSAGE)
    elif core_cache_success:
        warnings.append(f"部分核心行情使用本地缓存降级: {len(core_cache_success)} 项，请结合获取时间判断时效性。")
    elif failed:
        warnings.append(f"核心行情存在缺口: 失败 {failed} 项，但核心池可用率仍达到正式报告阈值。")
    if quote_only_tickers:
        warnings.append(f"部分标的历史指标暂缺，但 quote 可用: {', '.join(quote_only_tickers)}。")
    if extension_failed_assets:
        warnings.append("宏观资产部分暂缺；扩展池失败不阻止正式报告。")

    return {
        "source": source,
        "fetched_at": fetched_at,
        "total_count": total,
        "success_count": len(core_usable),
        "live_success_count": len(core_live_success),
        "cache_success_count": len(core_cache_success),
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
        "core_total_count": total,
        "core_success_count": len(core_usable),
        "core_failed_count": failed,
        "extension_total_count": len(extension_assets),
        "extension_success_count": len(extension_usable),
        "extension_failed_count": len(extension_failed_assets),
        "all_total_count": len(assets),
        "all_success_count": len(core_usable) + len(extension_usable),
        "all_failed_count": len(core_failed_assets) + len(extension_failed_assets),
        "success_tickers": success_tickers,
        "failed_tickers": failed_tickers,
        "failed_details": failed_details,
        "quote_success_tickers": quote_success_tickers,
        "historical_success_tickers": historical_success_tickers,
        "quote_only_tickers": quote_only_tickers,
        "extension_failed_details": extension_failed_details,
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
    if int(metadata.get("all_success_count") or metadata.get("success_count") or 0) <= 0:
        return
    path = Path(snapshot_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": metadata,
        "assets": [asset for asset in market_data.get("assets", []) if asset.get("last_close") is not None],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def build_provider_instances(
    provider_order: list[str],
    provider_options: dict[str, Any],
    universe: list[dict[str, Any]],
) -> dict[str, Any]:
    del universe
    instances: dict[str, Any] = {}
    if "fmp" not in provider_order:
        return instances

    try:
        provider = make_provider("fmp", provider_options_for(provider_options, "fmp"))
    except Exception:
        return instances

    # FMP batch quote is intentionally disabled for the formal daily run.
    # The server has shown partial failures through the batch endpoint, while
    # single-symbol stable/quote diagnostics succeed. Each ticker is fetched
    # one by one in fetch_history(), with request_delay_sec controlling pace.
    instances["fmp"] = provider
    return instances


def write_market_provider_diagnostics(market_data: dict[str, Any]) -> None:
    metadata = market_data.get("metadata", {})
    assets = market_data.get("assets", [])
    path = Path("logs/provider_check.log")
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"daily_market_run generated_at={datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"source={metadata.get('source', '')}",
        f"core_success={metadata.get('success_count', 0)}/{metadata.get('total_count', 0)} ratio={float(metadata.get('success_ratio') or 0.0) * 100:.1f}%",
        f"success_tickers={','.join(metadata.get('success_tickers', []))}",
        f"failed_tickers={','.join(metadata.get('failed_tickers', []))}",
        f"quote_success_tickers={','.join(metadata.get('quote_success_tickers', []))}",
        f"historical_success_tickers={','.join(metadata.get('historical_success_tickers', []))}",
        f"quote_only_tickers={','.join(metadata.get('quote_only_tickers', []))}",
    ]
    for asset in assets:
        source_info = asset.get("source", {}) if isinstance(asset.get("source"), dict) else {}
        status = "ok" if asset_is_usable(asset) else "failed"
        reason = asset.get("failure_reason") or asset.get("indicator_reason") or ""
        lines.append(
            " ".join(
                [
                    f"ticker={asset.get('ticker')}",
                    f"required={'yes' if asset.get('required', True) else 'no'}",
                    f"status={status}",
                    f"quote_success={'yes' if asset.get('quote_success') else 'no'}",
                    f"historical_success={'yes' if asset.get('historical_success') else 'no'}",
                    f"provider={source_info.get('provider') or ''}",
                    f"symbol={source_info.get('provider_symbol') or asset.get('ticker')}",
                    f"failure_reason={reason}",
                    f"as_of={asset.get('as_of') or ''}",
                ]
            )
        )

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
    provider_options = market.get("provider_options", {}) if isinstance(market.get("provider_options", {}), dict) else {}
    provider_order = effective_provider_order(configured_order, provider_options)
    source = provider_chain_source(provider_order)
    request_delay_sec = float(market.get("request_delay_sec", 0.3))
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
    provider_instances = build_provider_instances(provider_order, provider_options, universe)
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
            provider_instances,
        )
        summary = summarize_price_frame(frame)
        frame_attrs = getattr(frame, "attrs", {}) if frame is not None else {}
        quote_success = bool(frame_attrs.get("quote_success"))
        quote_only = bool(frame_attrs.get("fmp_quote_only"))
        historical_success = bool(summary.get("last_close") is not None and not quote_only)
        indicator_reason = "historical_failed" if quote_success and not historical_success else ""
        source_info = {
            "provider": actual_provider or source,
            "provider_chain": source,
            "ticker": asset["ticker"],
            "provider_symbol": frame_attrs.get("fmp_symbol") or asset["ticker"],
            "period": period,
            "interval": interval,
            "as_of": summary.get("as_of"),
            "fetched_at": fetched_at,
            "from_cache": from_cache,
            "quote_success": quote_success,
            "quote_only": quote_only,
            "historical_success": historical_success,
            "historical_error_category": frame_attrs.get("historical_error_category", ""),
            "historical_error": frame_attrs.get("historical_error", ""),
            "cache_path": str(cache_path(cache_dir, asset["ticker"], period, interval)) if from_cache else "",
        }
        asset_record = {
            **asset,
            **summary,
            "source": source_info,
            "error": error,
            "from_cache": from_cache,
            "cache_error": cache_error,
            "quote_success": quote_success,
            "quote_only": quote_only,
            "historical_success": historical_success,
            "indicator_reason": indicator_reason,
        }
        asset_record["failure_reason"] = classify_failure_reason(asset_record)
        assets.append(asset_record)

    quality = build_market_quality(assets, source, fetched_at, min_success_ratio)
    market_data = {
        "metadata": {
            "source": source,
            "configured_market_data_provider_order": configured_order,
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
    write_market_provider_diagnostics(market_data)
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
