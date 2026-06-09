from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .fetch_market import (
    configured_provider_order,
    effective_provider_order,
    normalize_provider_name,
    provider_options_for,
)
from .market_data_provider import MarketProviderError, make_provider, provider_is_enabled


EXPECTED_COMPANY_KEYWORDS: dict[str, list[str]] = {
    "NOW": ["servicenow"],
    "SNOW": ["snowflake"],
    "MU": ["micron"],
    "MRVL": ["marvell"],
    "ARM": ["arm"],
    "AVGO": ["broadcom"],
}

VALIDATION_PROVIDERS = {"fmp", "finnhub", "twelve_data"}


@dataclass
class SymbolValidationResult:
    ticker: str
    ok: bool
    provider: str = ""
    provider_symbol: str = ""
    company_name: str = ""
    exchange: str = ""
    currency: str = ""
    failure_reason: str = ""


def normalize_company_name(value: Any) -> str:
    return " ".join(str(value or "").lower().replace(".", " ").replace(",", " ").split())


def quote_metadata(quote: Any) -> dict[str, str]:
    raw = getattr(quote, "raw", {}) if quote is not None else {}
    raw = raw if isinstance(raw, dict) else {}
    company_name = (
        raw.get("name")
        or raw.get("companyName")
        or raw.get("company_name")
        or raw.get("shortName")
        or raw.get("longName")
        or raw.get("instrument_name")
        or raw.get("description")
        or ""
    )
    exchange = raw.get("exchange") or raw.get("exchangeShortName") or raw.get("mic_code") or raw.get("exchange_timezone") or ""
    currency = raw.get("currency") or raw.get("currency_name") or raw.get("currency_base") or ""
    return {"company_name": str(company_name or ""), "exchange": str(exchange or ""), "currency": str(currency or "")}


def company_matches(ticker: str, company_name: str) -> bool:
    expected = EXPECTED_COMPANY_KEYWORDS.get(ticker.upper(), [])
    if not expected:
        return True
    normalized = normalize_company_name(company_name)
    if not normalized:
        return True
    return any(keyword in normalized for keyword in expected)


def configured_validation_order(config: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    market = config.get("market", {})
    provider_options = market.get("provider_options", {}) if isinstance(market.get("provider_options", {}), dict) else {}
    order = [provider for provider in effective_provider_order(configured_provider_order(market), provider_options) if provider in VALIDATION_PROVIDERS]
    return order, provider_options


def validate_one_symbol(ticker: str, provider_order: list[str], provider_options: dict[str, Any]) -> SymbolValidationResult:
    last_error = "symbol_validation_unavailable"
    for provider_name in provider_order:
        normalized = normalize_provider_name(provider_name)
        options = provider_options_for(provider_options, normalized)
        if not provider_is_enabled(normalized, options):
            continue
        try:
            provider = make_provider(normalized, options)
            quote = provider.fetch_quote(ticker)
            metadata = quote_metadata(quote)
            company_name = metadata["company_name"]
            provider_symbol = getattr(quote, "symbol", "") or ticker
            if company_name and not company_matches(ticker, company_name):
                return SymbolValidationResult(ticker=ticker, ok=False, provider=getattr(provider, "name", normalized), provider_symbol=str(provider_symbol), company_name=company_name, exchange=metadata["exchange"], currency=metadata["currency"], failure_reason="symbol_mapping_error")
            return SymbolValidationResult(ticker=ticker, ok=True, provider=getattr(provider, "name", normalized), provider_symbol=str(provider_symbol), company_name=company_name, exchange=metadata["exchange"], currency=metadata["currency"], failure_reason="" if company_name else "company_name_unavailable")
        except MarketProviderError as exc:
            last_error = exc.category
        except Exception as exc:  # noqa: BLE001
            last_error = f"symbol_validation_failed: {exc}"
    return SymbolValidationResult(ticker=ticker, ok=False, failure_reason=last_error)


def validate_market_symbols(config: dict[str, Any], market_data: dict[str, Any]) -> dict[str, Any]:
    provider_order, provider_options = configured_validation_order(config)
    assets = market_data.get("assets", [])
    by_ticker = {str(asset.get("ticker", "")).upper(): asset for asset in assets}
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for ticker in EXPECTED_COMPANY_KEYWORDS:
        if ticker not in by_ticker:
            continue
        result = validate_one_symbol(ticker, provider_order, provider_options)
        payload = result.__dict__.copy()
        results.append(payload)
        asset = by_ticker.get(ticker)
        if asset is not None:
            source = asset.setdefault("source", {})
            if not isinstance(source, dict):
                source = {}
                asset["source"] = source
            source.update(
                {
                    "company_name": result.company_name,
                    "exchange": result.exchange,
                    "currency": result.currency,
                    "symbol_validation_provider": result.provider,
                    "symbol_validation_symbol": result.provider_symbol,
                    "symbol_validation_ok": result.ok,
                    "symbol_validation_failure_reason": result.failure_reason,
                }
            )
        if not result.ok and result.failure_reason == "symbol_mapping_error":
            errors.append(payload)

    metadata = market_data.setdefault("metadata", {})
    metadata["symbol_validation"] = {"checked": results, "errors": errors}
    if errors:
        blockers = metadata.setdefault("quality_blockers", [])
        for error in errors:
            text = f"symbol_mapping_error: {error.get('ticker')} -> {error.get('company_name') or 'unknown'}"
            if text not in blockers:
                blockers.append(text)
        failed_details = metadata.setdefault("failed_details", [])
        for error in errors:
            failed_details.append({"ticker": error.get("ticker"), "reason": "symbol_mapping_error", "quote_success": False, "historical_success": False, "provider": error.get("provider", ""), "provider_symbol": error.get("provider_symbol", ""), "company_name": error.get("company_name", "")})
        metadata["formal_report_allowed"] = False
        metadata["needs_data_source_upgrade"] = True
        warnings = metadata.setdefault("warnings", [])
        warning = "关键股票 symbol/company_name 校验失败，未生成正式报告。"
        if warning not in warnings:
            warnings.append(warning)
    return metadata.get("symbol_validation", {})
