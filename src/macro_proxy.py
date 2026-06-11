from __future__ import annotations

import copy
from typing import Any

from .fetch_market import DEFAULT_CRITICAL_GROUPS, asset_is_usable, build_market_quality


MACRO_PROXY_RULES: dict[str, dict[str, str]] = {
    "US10Y": {
        "proxy_ticker": "TLT",
        "theme": "利率/替代口径",
        "name": "10Y美债收益率数据暂缺，使用TLT观察长端利率方向。",
        "note": "10Y美债收益率数据暂缺，使用TLT观察长端利率方向。",
        "provider_label": "TLT proxy",
    },
    "DXY": {
        "proxy_ticker": "UUP",
        "theme": "美元/替代口径",
        "name": "美元指数数据暂缺，使用UUP观察美元方向。",
        "note": "美元指数数据暂缺，使用UUP观察美元方向。",
        "provider_label": "UUP proxy",
    },
}


def asset_lookup(market_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(asset.get("ticker", "")).upper(): asset for asset in market_data.get("assets", [])}


def replace_asset(assets: list[dict[str, Any]], ticker: str, replacement: dict[str, Any]) -> None:
    for index, asset in enumerate(assets):
        if str(asset.get("ticker", "")).upper() == ticker:
            assets[index] = replacement
            return
    assets.append(replacement)


def build_proxy_asset(target_ticker: str, target_asset: dict[str, Any] | None, proxy_asset: dict[str, Any], rule: dict[str, str]) -> dict[str, Any]:
    proxy_ticker = rule["proxy_ticker"]
    record = copy.deepcopy(proxy_asset)
    original = target_asset or {}
    source = copy.deepcopy(record.get("source", {})) if isinstance(record.get("source"), dict) else {}
    upstream_provider = source.get("provider") or record.get("source", {}).get("provider") or "market data"

    record.update(
        {
            "ticker": target_ticker,
            "name": rule["name"],
            "category": original.get("category") or "macro_asset",
            "theme": rule["theme"],
            "required": bool(original.get("required", True)),
            "macro_proxy": True,
            "macro_proxy_note": rule["note"],
            "proxy_ticker": proxy_ticker,
            "error": "",
            "failure_reason": "",
        }
    )
    source.update(
        {
            "provider": f"{rule['provider_label']} via {upstream_provider}",
            "ticker": target_ticker,
            "provider_symbol": proxy_ticker,
            "proxy_for": target_ticker,
            "proxy_ticker": proxy_ticker,
            "macro_proxy": True,
            "macro_proxy_note": rule["note"],
        }
    )
    record["source"] = source
    return record


def refresh_quality(config: dict[str, Any], market_data: dict[str, Any]) -> None:
    metadata = market_data.setdefault("metadata", {})
    market = config.get("market", {}) if isinstance(config.get("market"), dict) else {}
    critical_groups = market.get("critical_groups") if isinstance(market.get("critical_groups"), dict) else DEFAULT_CRITICAL_GROUPS
    min_success_ratio = float(market.get("min_success_ratio", metadata.get("min_success_ratio", 0.9)))
    quality = build_market_quality(
        market_data.get("assets", []),
        str(metadata.get("source") or ""),
        str(metadata.get("fetched_at") or ""),
        min_success_ratio,
        critical_groups,
    )
    metadata.update(quality)


def apply_macro_proxies(config: dict[str, Any], market_data: dict[str, Any]) -> dict[str, Any]:
    assets = market_data.setdefault("assets", [])
    lookup = asset_lookup(market_data)
    applied: list[dict[str, str]] = []

    for target_ticker, rule in MACRO_PROXY_RULES.items():
        target_asset = lookup.get(target_ticker)
        if asset_is_usable(target_asset):
            continue
        proxy_ticker = rule["proxy_ticker"]
        proxy_asset = lookup.get(proxy_ticker)
        if not asset_is_usable(proxy_asset):
            continue
        replacement = build_proxy_asset(target_ticker, target_asset, proxy_asset, rule)
        replace_asset(assets, target_ticker, replacement)
        lookup[target_ticker] = replacement
        applied.append({"ticker": target_ticker, "proxy_ticker": proxy_ticker, "note": rule["note"]})

    if applied:
        metadata = market_data.setdefault("metadata", {})
        metadata["macro_proxy_applied"] = applied
        metadata["macro_proxy_tickers"] = [f"{item['ticker']}->{item['proxy_ticker']}" for item in applied]
        metadata["macro_proxy_notes"] = [item["note"] for item in applied]
        refresh_quality(config, market_data)
    return market_data
