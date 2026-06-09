from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .fetch_market import (
    cache_path,
    call_with_timeout,
    configured_provider_order,
    critical_group_results,
    effective_provider_order,
    error_category,
    load_cache,
    load_config,
    provider_option_enabled,
    provider_options_for,
    universe_from_config,
)
from .market_data_provider import make_provider, provider_display_name, provider_is_enabled, resolve_provider_symbol


DEFAULT_FORMAL_TICKERS = [
    "SPY", "QQQ", "DIA", "IWM", "SMH", "SOXX", "XLK", "XLF", "XLE", "XLV", "TLT", "GLD", "USO",
    "VIX", "US10Y", "DXY", "CPER", "BTCUSD", "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "AVGO", "MU", "MRVL", "ARM", "SNOW", "NOW",
]
LIVE_PROVIDERS = ["fmp", "finnhub", "twelve_data", "stooq", "yfinance"]
ALL_PROVIDERS = ["fmp", "finnhub", "twelve_data", "stooq", "yfinance", "cache"]
HISTORICAL_SAMPLE = ["AAPL", "MSFT", "NVDA"]
ALLOWED_FAILURE_REASONS = {
    "missing_api_key", "invalid_api_key", "payment_required", "provider_permission_denied", "quote_failed", "historical_failed",
    "symbol_not_supported", "permission_denied", "rate_limited", "network_error", "schema_parse_error", "empty_response", "provider_timeout",
}


def log_path() -> Path:
    path = Path("logs/provider_check.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def truncate_reason(reason: str, limit: int = 500) -> str:
    clean = " ".join(str(reason).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def normalize_failure_category(category: str | None, reason: str | None = None) -> str:
    category_text = str(category or "").strip().lower()
    reason_text = str(reason or "").strip().lower()
    combined = f"{category_text} {reason_text}"
    if category_text in ALLOWED_FAILURE_REASONS:
        return category_text
    if "402" in combined or "payment_required" in combined:
        return "payment_required"
    if "api key" in combined and ("missing" in combined or "not configured" in combined or "empty" in combined):
        return "missing_api_key"
    if "429" in combined or "rate limit" in combined or "too many requests" in combined:
        return "rate_limited"
    if "403" in combined or "provider_permission" in combined or "permission" in combined or "forbidden" in combined:
        return "provider_permission_denied"
    if "network" in combined or "connection" in combined or "timeout" in combined or "temporarily unavailable" in combined:
        return "provider_timeout" if "timeout" in combined else "network_error"
    if "schema" in combined or "parse" in combined or "missing price" in combined:
        return "schema_parse_error"
    if "no data" in combined or "not supported" in combined or "not found" in combined or "invalid symbol" in combined:
        return "symbol_not_supported"
    if "empty" in combined or "no cache fallback available" in combined or "cache is empty" in combined:
        return "empty_response"
    return "quote_failed"


def parse_tickers(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip().upper() for item in value.split(",") if item.strip()]


def normalize_provider_name(provider: str) -> str:
    normalized = provider.strip().lower()
    aliases = {"financial_modeling_prep": "fmp", "financialmodelingprep": "fmp", "finn_hub": "finnhub", "twelvedata": "twelve_data", "twelve": "twelve_data", "local_cache": "cache", "local market cache": "cache"}
    return aliases.get(normalized, normalized)


def providers_to_check(provider: str) -> list[str]:
    normalized = normalize_provider_name(provider)
    if normalized == "all":
        return ALL_PROVIDERS.copy()
    if normalized in ALL_PROVIDERS:
        return [normalized]
    raise ValueError("--provider must be one of: all, fmp, finnhub, twelve_data, stooq, yfinance, cache")


def provider_enabled(provider_name: str, provider_options: dict[str, Any]) -> bool:
    options = provider_options_for(provider_options, provider_name)
    return provider_option_enabled(provider_name, options) and provider_is_enabled(provider_name, options)


def disabled_reason(provider_name: str, provider_options: dict[str, Any]) -> tuple[str, str]:
    options = provider_options_for(provider_options, provider_name)
    if options.get("enabled") is False or (provider_name == "stooq" and not provider_option_enabled(provider_name, options)):
        return "provider disabled by config", "provider_disabled"
    env_names = {"fmp": "FMP_API_KEY", "finnhub": "FINNHUB_API_KEY", "twelve_data": "TWELVE_DATA_API_KEY"}
    if provider_name in env_names:
        return f"{env_names[provider_name]} is not configured", "missing_api_key"
    return "provider is disabled", "quote_failed"


def disabled_provider_result(provider_name: str, tickers: list[str], provider_options: dict[str, Any]) -> dict[str, Any]:
    reason, failure_category = disabled_reason(provider_name, provider_options)
    rows = [{"ticker": ticker, "symbol": resolve_provider_symbol(provider_name, ticker, provider_options_for(provider_options, provider_name)), "status": "disabled", "rows": 0, "latest": "", "reason": reason, "failure_category": failure_category, "quote_parse_success": False, "historical_parse_success": False, "historical_failure_category": ""} for ticker in tickers]
    return {"provider": provider_display_name(provider_name), "provider_key": provider_name, "enabled": False, "success_count": 0, "failed_count": len(tickers), "items": rows}


def api_key_metadata(provider: Any) -> dict[str, Any]:
    metadata = getattr(provider, "api_key_metadata", None)
    if callable(metadata):
        return metadata()
    return {}


def provider_quote_check(provider: Any, ticker: str, period: str, interval: str, timeout_sec: int) -> tuple[bool, bool, int, str, str]:
    fetch_quote = getattr(provider, "fetch_quote", None)
    if callable(fetch_quote):
        try:
            call_with_timeout(lambda: fetch_quote(ticker), timeout_sec, f"{provider.name} quote {ticker}")
            historical_ok = False
            if ticker in HISTORICAL_SAMPLE:
                try:
                    frame = call_with_timeout(lambda: provider.fetch_history(ticker, period, interval), timeout_sec, f"{provider.name} historical {ticker}")
                    historical_ok = frame is not None and not frame.empty
                except Exception:
                    historical_ok = False
            return True, historical_ok, 1, "", ""
        except Exception as exc:  # noqa: BLE001
            category = normalize_failure_category(error_category(exc), str(exc))
            return False, False, 0, truncate_reason(str(exc)), category
    try:
        frame = call_with_timeout(lambda: provider.fetch_history(ticker, "5d", interval), timeout_sec, f"{provider.name} proxy {ticker}")
        if frame is None or frame.empty:
            raise ValueError("empty price history")
        return True, True, int(len(frame)), "", ""
    except Exception as exc:  # noqa: BLE001
        category = normalize_failure_category(error_category(exc), str(exc))
        return False, False, 0, truncate_reason(str(exc)), category


def check_dry_provider(provider_name: str, tickers: list[str], provider_options: dict[str, Any], cache_dir: str, period: str, interval: str) -> dict[str, Any]:
    enabled = provider_name == "cache" or provider_enabled(provider_name, provider_options)
    rows: list[dict[str, Any]] = []
    for ticker in tickers:
        symbol = str(cache_path(cache_dir, ticker, period, interval)) if provider_name == "cache" else resolve_provider_symbol(provider_name, ticker, provider_options_for(provider_options, provider_name))
        reason, category = disabled_reason(provider_name, provider_options) if not enabled else ("", "")
        rows.append({"ticker": ticker, "symbol": symbol, "status": "mapped" if enabled else "disabled", "rows": 0, "latest": "", "reason": reason, "failure_category": category, "quote_parse_success": enabled, "historical_parse_success": False, "historical_failure_category": ""})
    return {"provider": provider_display_name(provider_name), "provider_key": provider_name, "enabled": enabled, "dry_run": True, "success_count": len(tickers) if enabled else 0, "failed_count": 0 if enabled else len(tickers), "items": rows}


def check_live_provider(provider_name: str, tickers: list[str], period: str, interval: str, provider_options: dict[str, Any], request_delay_sec: float, timeout_sec: int) -> dict[str, Any]:
    if not provider_enabled(provider_name, provider_options):
        return disabled_provider_result(provider_name, tickers, provider_options)
    options = provider_options_for(provider_options, provider_name)
    try:
        provider = make_provider(provider_name, options)
    except Exception as exc:  # noqa: BLE001
        reason = truncate_reason(str(exc))
        category = normalize_failure_category("", reason)
        return {"provider": provider_display_name(provider_name), "provider_key": provider_name, "enabled": True, "success_count": 0, "failed_count": len(tickers), "items": [{"ticker": ticker, "symbol": resolve_provider_symbol(provider_name, ticker, options), "status": "failed", "rows": 0, "latest": "", "reason": reason, "failure_category": category, "quote_parse_success": False, "historical_parse_success": False, "historical_failure_category": ""} for ticker in tickers]}

    rows: list[dict[str, Any]] = []
    success = 0
    failed = 0
    for index, ticker in enumerate(tickers):
        if index > 0 and request_delay_sec > 0:
            time.sleep(request_delay_sec)
        symbol = resolve_provider_symbol(provider_name, ticker, options)
        quote_ok, historical_ok, row_count, reason, category = provider_quote_check(provider, ticker, period, interval, timeout_sec)
        if quote_ok:
            success += 1
            rows.append({"ticker": ticker, "symbol": symbol, "status": "ok", "rows": row_count, "latest": "", "reason": "", "failure_category": "", "quote_parse_success": True, "historical_parse_success": historical_ok, "historical_failure_category": "" if historical_ok or ticker not in HISTORICAL_SAMPLE else "historical_failed"})
        else:
            failed += 1
            rows.append({"ticker": ticker, "symbol": symbol, "status": "failed", "rows": 0, "latest": "", "reason": reason or category, "failure_category": category or "quote_failed", "quote_parse_success": False, "historical_parse_success": False, "historical_failure_category": ""})
    return {"provider": provider_display_name(provider_name), "provider_key": provider_name, "enabled": True, "api_key": api_key_metadata(provider), "success_count": success, "failed_count": failed, "items": rows}


def check_cache_provider(tickers: list[str], period: str, interval: str, cache_dir: str, cache_max_age_hours: int, cache_max_trading_days: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    success = 0
    failed = 0
    for ticker in tickers:
        path = cache_path(cache_dir, ticker, period, interval)
        frame, error = load_cache(path, cache_max_age_hours, cache_max_trading_days)
        if not frame.empty:
            success += 1
            rows.append({"ticker": ticker, "symbol": str(path), "status": "ok", "rows": int(len(frame)), "latest": str(frame.index.max()), "reason": "", "failure_category": "", "quote_parse_success": True, "historical_parse_success": True, "historical_failure_category": ""})
        else:
            failed += 1
            reason = truncate_reason(error or "no cache fallback available")
            rows.append({"ticker": ticker, "symbol": str(path), "status": "failed", "rows": 0, "latest": "", "reason": reason, "failure_category": normalize_failure_category("", reason), "quote_parse_success": False, "historical_parse_success": False, "historical_failure_category": ""})
    return {"provider": "Local market cache", "provider_key": "cache", "enabled": True, "success_count": success, "failed_count": failed, "items": rows}


def chain_coverage(results: list[dict[str, Any]], provider_order: list[str], tickers: list[str], critical_groups: dict[str, Any]) -> dict[str, Any]:
    by_provider = {result["provider_key"]: {item["ticker"]: item for item in result.get("items", [])} for result in results}
    success_tickers: list[str] = []
    failed_details: list[dict[str, Any]] = []
    provider_counts: dict[str, int] = {}
    for ticker in tickers:
        chosen: tuple[str, dict[str, Any]] | None = None
        failure_reason = "quote_failed"
        for provider_name in provider_order:
            item = by_provider.get(provider_name, {}).get(ticker)
            if not item:
                continue
            if item.get("status") in {"ok", "mapped"}:
                chosen = (provider_name, item)
                break
            failure_reason = item.get("failure_category") or item.get("reason") or failure_reason
        if chosen:
            provider_name, _ = chosen
            success_tickers.append(ticker)
            display = provider_display_name(provider_name)
            provider_counts[display] = provider_counts.get(display, 0) + 1
        else:
            failed_details.append({"ticker": ticker, "reason": failure_reason, "quote_success": False, "historical_success": False})
    total = len(tickers)
    success_ratio = len(success_tickers) / total if total else 0.0
    pseudo_assets = [{"ticker": ticker, "last_close": 1.0 if ticker in success_tickers else None, "daily_change": 0.0 if ticker in success_tickers else None, "quote_success": ticker in success_tickers, "required": True} for ticker in tickers]
    groups = critical_group_results(pseudo_assets, critical_groups)
    return {"total_count": total, "success_count": len(success_tickers), "failed_count": len(failed_details), "success_ratio": success_ratio, "success_tickers": success_tickers, "failed_tickers": [item["ticker"] for item in failed_details], "failed_details": failed_details, "provider_counts": provider_counts, "critical_groups": groups, "critical_groups_passed": all(group.get("passed") for group in groups.values())}


def render_text(results: list[dict[str, Any]], coverage: dict[str, Any], generated_at: str, period: str, interval: str, tickers: list[str], dry_run: bool) -> str:
    mode = "dry_mapping" if dry_run else "live_quote"
    lines = [f"provider_check mode={mode} generated_at={generated_at} period={period} interval={interval} full_pool_total={len(tickers)} tickers={','.join(tickers)}"]
    lines.append(f"chain_success={coverage.get('success_count', 0)}/{coverage.get('total_count', 0)} ratio={float(coverage.get('success_ratio') or 0.0) * 100:.1f}%")
    lines.append(f"chain_success_tickers={','.join(coverage.get('success_tickers', []))}")
    failed_text = ",".join(f"{item['ticker']}({item.get('reason') or 'quote_failed'})" for item in coverage.get("failed_details", []))
    lines.append(f"chain_failed_tickers={failed_text}")
    lines.append(f"chain_provider_counts={json.dumps(coverage.get('provider_counts', {}), ensure_ascii=False)}")
    lines.append(f"critical_groups_passed={str(coverage.get('critical_groups_passed')).lower()} critical_groups={json.dumps(coverage.get('critical_groups', {}), ensure_ascii=False)}")
    for result in results:
        items = result["items"]
        success_tickers = [str(item["ticker"]) for item in items if item.get("status") in {"ok", "mapped"}]
        failed_items = [item for item in items if item.get("status") not in {"ok", "mapped"}]
        failed_provider_text = ",".join(f"{item['ticker']}({item.get('failure_category') or item.get('reason') or 'quote_failed'})" for item in failed_items)
        quote_success = [str(item["ticker"]) for item in items if item.get("quote_parse_success")]
        historical_success = [str(item["ticker"]) for item in items if item.get("historical_parse_success")]
        api_key = result.get("api_key") or {}
        if result.get("provider_key") in {"fmp", "finnhub", "twelve_data"} and api_key:
            lines.append(f"{api_key.get('env_name')} exists={'yes' if api_key.get('exists') else 'no'} length={api_key.get('length', 0)} trimmed_length={api_key.get('trimmed_length', 0)} outer_whitespace={'yes' if api_key.get('has_outer_whitespace') else 'no'} loaded_from_dotenv={'yes' if api_key.get('loaded_from_dotenv') else 'no'}")
        lines.append(f"provider={result['provider']} enabled={str(result.get('enabled', True)).lower()} success={result['success_count']} failed={result['failed_count']}")
        lines.append(f"success_tickers={','.join(success_tickers)}")
        lines.append(f"failed_tickers={failed_provider_text}")
        lines.append(f"quote_success_tickers={','.join(quote_success)}")
        lines.append(f"historical_sample_success_tickers={','.join(historical_success)}")
        for item in items:
            reason = f" reason={item['reason']}" if item.get("reason") else ""
            category = f" failure_category={item['failure_category']}" if item.get("failure_category") else ""
            historical_category = f" historical_failure_category={item['historical_failure_category']}" if item.get("historical_failure_category") else ""
            lines.append(f"  ticker={item['ticker']} symbol={item['symbol']} status={item['status']} rows={item['rows']} latest={item['latest']} quote_success={'yes' if item.get('quote_parse_success') else 'no'} historical_success={'yes' if item.get('historical_parse_success') else 'no'}{category}{historical_category}{reason}")
    return "\n".join(lines) + "\n"


def write_log(text: str) -> None:
    path = log_path()
    if path.exists() and path.stat().st_size > 0:
        with path.open("a", encoding="utf-8") as file:
            file.write("\n---\n")
            file.write(text)
    else:
        path.write_text(text, encoding="utf-8")


def fmp_result_failed(results: list[dict[str, Any]], selected_providers: list[str]) -> bool:
    if selected_providers != ["fmp"]:
        return False
    for result in results:
        if result.get("provider_key") == "fmp":
            return int(result.get("success_count") or 0) == 0
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Check market data providers against the formal report ticker universe.")
    parser.add_argument("--provider", default="all", help="all, fmp, finnhub, twelve_data, stooq, yfinance, or cache")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker list. Defaults to the formal report universe in config.yaml.")
    parser.add_argument("--symbols", default="", help="Alias for --tickers, useful for provider symbol diagnostics.")
    parser.add_argument("--universe", action="store_true", help="Use the formal report ticker universe from config.yaml.")
    parser.add_argument("--dry-run", action="store_true", help="Check full-pool symbol mapping and provider enablement without live quote requests.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args()

    config = load_config(args.config)
    market = config.get("market", {})
    provider_options = market.get("provider_options", {}) if isinstance(market.get("provider_options", {}), dict) else {}
    explicit_tickers = parse_tickers(args.symbols or args.tickers)
    tickers = explicit_tickers or [str(item["ticker"]) for item in universe_from_config(config)] or DEFAULT_FORMAL_TICKERS.copy()
    selected_providers = providers_to_check(args.provider)
    configured_order = effective_provider_order(configured_provider_order(market), provider_options)
    provider_order_for_coverage = [provider for provider in configured_order if provider in selected_providers] or selected_providers
    period = str(market.get("provider_check_period", market.get("period", "90d")))
    interval = str(market.get("interval", "1d"))
    request_delay_sec = float(market.get("provider_check_request_delay_sec", 0.05))
    timeout_sec = int(market.get("provider_check_request_timeout_sec", min(int(market.get("provider_request_timeout_sec", 12)), 8)))
    cache_dir = str(market.get("cache_dir", "data/processed/market_cache"))
    cache_max_age_hours = int(market.get("cache_max_age_hours", 168))
    cache_max_trading_days = int(market.get("cache_max_trading_days", 3))
    critical_groups = market.get("critical_groups") if isinstance(market.get("critical_groups"), dict) else {}
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    results: list[dict[str, Any]] = []
    for provider_name in selected_providers:
        if args.dry_run:
            results.append(check_dry_provider(provider_name, tickers, provider_options, cache_dir, period, interval))
        elif provider_name in LIVE_PROVIDERS:
            results.append(check_live_provider(provider_name, tickers, period, interval, provider_options, request_delay_sec, timeout_sec))
        elif provider_name == "cache":
            results.append(check_cache_provider(tickers, period, interval, cache_dir, cache_max_age_hours, cache_max_trading_days))
    coverage = chain_coverage(results, provider_order_for_coverage, tickers, critical_groups)
    payload = {"generated_at": generated_at, "mode": "dry_mapping" if args.dry_run else "live_quote", "config": args.config, "configured_provider_order": configured_provider_order(market), "provider_order_for_coverage": provider_order_for_coverage, "tickers": tickers, "period": period, "interval": interval, "coverage": coverage, "results": results}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n" if args.json else render_text(results, coverage, generated_at, period, interval, tickers, args.dry_run)
    write_log(text)
    print(text, end="")
    if not args.dry_run and fmp_result_failed(results, selected_providers):
        sys.exit(2)


if __name__ == "__main__":
    main()
