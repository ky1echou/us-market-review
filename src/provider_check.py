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
    configured_provider_order,
    load_cache,
    load_config,
    provider_options_for,
    universe_from_config,
)
from .market_data_provider import make_provider, provider_display_name, provider_is_enabled, resolve_provider_symbol


CORE_TICKERS = ["SPY", "QQQ", "DIA", "IWM", "NVDA", "MSFT", "AAPL", "AMD", "AVGO", "TSLA"]
LIVE_PROVIDERS = ["fmp", "stooq", "yfinance"]
ALL_PROVIDERS = ["fmp", "stooq", "yfinance", "cache"]
ALLOWED_FAILURE_REASONS = {
    "quote_failed",
    "historical_failed",
    "symbol_not_supported",
    "permission_denied",
    "rate_limited",
    "schema_parse_error",
    "empty_response",
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


def parse_tickers(value: str | None) -> list[str]:
    if not value:
        return CORE_TICKERS.copy()
    tickers = [item.strip().upper() for item in value.split(",") if item.strip()]
    return tickers or CORE_TICKERS.copy()


def normalize_provider_name(provider: str) -> str:
    normalized = provider.strip().lower()
    aliases = {
        "financial_modeling_prep": "fmp",
        "financialmodelingprep": "fmp",
        "local_cache": "cache",
    }
    return aliases.get(normalized, normalized)


def providers_to_check(provider: str) -> list[str]:
    normalized = normalize_provider_name(provider)
    if normalized == "all":
        return ALL_PROVIDERS.copy()
    if normalized in ALL_PROVIDERS:
        return [normalized]
    raise ValueError("--provider must be one of: all, fmp, stooq, yfinance, cache")


def disabled_provider_result(provider_name: str, tickers: list[str], provider_options: dict[str, Any]) -> dict[str, Any]:
    rows = []
    reason = "FMP_API_KEY is not configured" if provider_name == "fmp" else "provider is disabled"
    failure_category = "missing_api_key" if provider_name == "fmp" else "quote_failed"
    for ticker in tickers:
        rows.append(
            {
                "ticker": ticker,
                "symbol": resolve_provider_symbol(provider_name, ticker, provider_options_for(provider_options, provider_name)),
                "status": "disabled",
                "rows": 0,
                "latest": "",
                "reason": reason,
                "failure_category": failure_category,
                "quote_parse_success": False,
                "historical_parse_success": False,
                "historical_failure_category": "",
            }
        )
    return {
        "provider": provider_display_name(provider_name),
        "provider_key": provider_name,
        "enabled": False,
        "success_count": 0,
        "failed_count": len(tickers),
        "items": rows,
    }


def historical_failure_from_endpoints(endpoints: list[dict[str, Any]]) -> str:
    for endpoint in endpoints:
        if endpoint.get("endpoint_type") == "historical_eod" and not endpoint.get("parse_success"):
            return normalize_failure_category(endpoint.get("failure_category"), endpoint.get("failure_reason"))
    return ""


def check_fmp_provider(
    tickers: list[str],
    provider_options: dict[str, Any],
    request_delay_sec: float,
) -> dict[str, Any]:
    options = provider_options_for(provider_options, "fmp")
    provider = make_provider("fmp", options)
    rows: list[dict[str, Any]] = []
    success = 0
    failed = 0

    for index, ticker in enumerate(tickers):
        if index > 0 and request_delay_sec > 0:
            time.sleep(request_delay_sec)
        diagnostic = getattr(provider, "diagnose_symbol")(ticker)
        quote_ok = bool(diagnostic.get("quote_parse_success"))
        historical_ok = bool(diagnostic.get("historical_parse_success"))
        endpoints = diagnostic.get("endpoints", [])
        historical_failure_category = historical_failure_from_endpoints(endpoints)
        if quote_ok:
            success += 1
            status = "ok"
            reason = ""
            failure_category = ""
        else:
            failed += 1
            status = "failed"
            failure_category = normalize_failure_category(diagnostic.get("failure_category"), "")
            reason = failure_category
        rows.append(
            {
                "ticker": ticker,
                "symbol": diagnostic.get("symbol", ticker),
                "status": status,
                "rows": 1 if status == "ok" else 0,
                "latest": "",
                "reason": reason,
                "failure_category": failure_category,
                "quote_parse_success": quote_ok,
                "historical_parse_success": historical_ok,
                "historical_failure_category": historical_failure_category,
                "endpoints": endpoints,
            }
        )

    api_key_meta = getattr(provider, "api_key_metadata")()
    return {
        "provider": provider_display_name("fmp"),
        "provider_key": "fmp",
        "enabled": bool(api_key_meta.get("exists")),
        "api_key": api_key_meta,
        "success_count": success,
        "failed_count": failed,
        "items": rows,
    }


def check_live_provider(
    provider_name: str,
    tickers: list[str],
    period: str,
    interval: str,
    provider_options: dict[str, Any],
    request_delay_sec: float,
) -> dict[str, Any]:
    if provider_name == "fmp":
        return check_fmp_provider(tickers, provider_options, request_delay_sec)

    options = provider_options_for(provider_options, provider_name)
    if not provider_is_enabled(provider_name, options):
        return disabled_provider_result(provider_name, tickers, provider_options)

    rows: list[dict[str, Any]] = []
    success = 0
    failed = 0

    try:
        provider = make_provider(provider_name, options)
    except Exception as exc:  # noqa: BLE001 - diagnostic should continue.
        reason = truncate_reason(str(exc))
        failure_category = normalize_failure_category("", reason)
        return {
            "provider": provider_display_name(provider_name),
            "provider_key": provider_name,
            "enabled": True,
            "success_count": 0,
            "failed_count": len(tickers),
            "items": [
                {
                    "ticker": ticker,
                    "symbol": resolve_provider_symbol(provider_name, ticker, options),
                    "status": "failed",
                    "rows": 0,
                    "latest": "",
                    "reason": reason,
                    "failure_category": failure_category,
                    "quote_parse_success": False,
                    "historical_parse_success": False,
                    "historical_failure_category": "",
                }
                for ticker in tickers
            ],
        }

    for index, ticker in enumerate(tickers):
        if index > 0 and request_delay_sec > 0:
            time.sleep(request_delay_sec)
        symbol = resolve_provider_symbol(provider_name, ticker, options)
        try:
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
                    "failure_category": "",
                    "quote_parse_success": True,
                    "historical_parse_success": True,
                    "historical_failure_category": "",
                }
            )
        except Exception as exc:  # noqa: BLE001 - diagnostic should continue.
            failed += 1
            reason = truncate_reason(str(exc))
            rows.append(
                {
                    "ticker": ticker,
                    "symbol": symbol,
                    "status": "failed",
                    "rows": 0,
                    "latest": "",
                    "reason": reason,
                    "failure_category": normalize_failure_category("", reason),
                    "quote_parse_success": False,
                    "historical_parse_success": False,
                    "historical_failure_category": "",
                }
            )
    return {
        "provider": provider_display_name(provider_name),
        "provider_key": provider_name,
        "enabled": True,
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
                    "failure_category": "",
                    "quote_parse_success": True,
                    "historical_parse_success": True,
                    "historical_failure_category": "",
                }
            )
        else:
            failed += 1
            reason = truncate_reason(error or "no cache fallback available")
            rows.append(
                {
                    "ticker": ticker,
                    "symbol": str(path),
                    "status": "failed",
                    "rows": 0,
                    "latest": "",
                    "reason": reason,
                    "failure_category": normalize_failure_category("", reason),
                    "quote_parse_success": False,
                    "historical_parse_success": False,
                    "historical_failure_category": "",
                }
            )
    return {
        "provider": "Local market cache",
        "provider_key": "cache",
        "enabled": True,
        "success_count": success,
        "failed_count": failed,
        "items": rows,
    }


def render_text(results: list[dict[str, Any]], generated_at: str, period: str, interval: str, tickers: list[str]) -> str:
    lines = [f"provider_check generated_at={generated_at} period={period} interval={interval} tickers={','.join(tickers)}"]
    for result in results:
        items = result["items"]
        success_tickers = [str(item["ticker"]) for item in items if item.get("status") == "ok"]
        failed_items = [item for item in items if item.get("status") != "ok"]
        failed_text = ",".join(f"{item['ticker']}({item.get('failure_category') or item.get('reason') or 'quote_failed'})" for item in failed_items)
        quote_success = [str(item["ticker"]) for item in items if item.get("quote_parse_success")]
        historical_success = [str(item["ticker"]) for item in items if item.get("historical_parse_success")]
        api_key = result.get("api_key") or {}
        if result.get("provider_key") == "fmp":
            lines.append(
                "FMP_API_KEY "
                f"exists={'yes' if api_key.get('exists') else 'no'} "
                f"length={api_key.get('length', 0)} "
                f"trimmed_length={api_key.get('trimmed_length', 0)} "
                f"outer_whitespace={'yes' if api_key.get('has_outer_whitespace') else 'no'} "
                f"loaded_from_dotenv={'yes' if api_key.get('loaded_from_dotenv') else 'no'}"
            )
        lines.append(
            f"provider={result['provider']} enabled={str(result.get('enabled', True)).lower()} "
            f"success={result['success_count']} failed={result['failed_count']}"
        )
        lines.append(f"success_tickers={','.join(success_tickers)}")
        lines.append(f"failed_tickers={failed_text}")
        lines.append(f"quote_success_tickers={','.join(quote_success)}")
        lines.append(f"historical_success_tickers={','.join(historical_success)}")
        for item in items:
            reason = f" reason={item['reason']}" if item.get("reason") else ""
            category = f" failure_category={item['failure_category']}" if item.get("failure_category") else ""
            historical_category = (
                f" historical_failure_category={item['historical_failure_category']}"
                if item.get("historical_failure_category")
                else ""
            )
            lines.append(
                f"  ticker={item['ticker']} symbol={item['symbol']} status={item['status']} rows={item['rows']} latest={item['latest']}"
                f" quote_success={'yes' if item.get('quote_parse_success') else 'no'}"
                f" historical_success={'yes' if item.get('historical_parse_success') else 'no'}"
                f"{category}{historical_category}{reason}"
            )
            if result.get("provider_key") == "fmp":
                for endpoint in item.get("endpoints", []):
                    lines.append(
                        "    endpoint="
                        f"{endpoint.get('endpoint_type')} "
                        f"url={endpoint.get('url')} "
                        f"http_status={endpoint.get('http_status')} "
                        f"json_type={endpoint.get('json_type')} "
                        f"parse_success={'yes' if endpoint.get('parse_success') else 'no'} "
                        f"failure_category={endpoint.get('failure_category', '')} "
                        f"preview={endpoint.get('preview', '')}"
                    )
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
    parser = argparse.ArgumentParser(description="Check FMP, Stooq, yfinance, and local cache with a small ticker set.")
    parser.add_argument("--provider", default="all", help="all, fmp, stooq, yfinance, or cache")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--tickers", default="", help="Optional comma-separated ticker list. Defaults to 10 core tickers.")
    parser.add_argument("--symbols", default="", help="Alias for --tickers, useful for FMP symbol diagnostics.")
    parser.add_argument("--universe", action="store_true", help="Use the formal report ticker universe from config.yaml.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of text.")
    args = parser.parse_args()

    config = load_config(args.config)
    market = config.get("market", {})
    provider_options = market.get("provider_options", {}) if isinstance(market.get("provider_options", {}), dict) else {}
    if args.universe:
        tickers = [str(item["ticker"]) for item in universe_from_config(config)]
    else:
        tickers = parse_tickers(args.symbols or args.tickers)
    selected_providers = providers_to_check(args.provider)
    period = str(market.get("provider_check_period", market.get("period", "90d")))
    interval = str(market.get("interval", "1d"))
    request_delay_sec = float(market.get("request_delay_sec", 0.3))
    cache_dir = str(market.get("cache_dir", "data/processed/market_cache"))
    cache_max_age_hours = int(market.get("cache_max_age_hours", 168))
    cache_max_trading_days = int(market.get("cache_max_trading_days", 3))
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")

    results: list[dict[str, Any]] = []
    for provider_name in selected_providers:
        if provider_name in LIVE_PROVIDERS:
            results.append(check_live_provider(provider_name, tickers, period, interval, provider_options, request_delay_sec))
        elif provider_name == "cache":
            results.append(check_cache_provider(tickers, period, interval, cache_dir, cache_max_age_hours, cache_max_trading_days))

    payload = {
        "generated_at": generated_at,
        "config": args.config,
        "configured_provider_order": configured_provider_order(market),
        "tickers": tickers,
        "period": period,
        "interval": interval,
        "results": results,
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n" if args.json else render_text(results, generated_at, period, interval, tickers)
    write_log(text)
    print(text, end="")
    if fmp_result_failed(results, selected_providers):
        sys.exit(2)


if __name__ == "__main__":
    main()
