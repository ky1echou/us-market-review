from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import feedparser
import requests

from .fetch_market import load_config, now_iso


AI_COMPANIES = {
    "openai", "anthropic", "google", "alphabet", "gemini", "deepmind", "meta", "microsoft", "azure",
    "amazon", "aws", "nvidia", "nvda", "amd", "broadcom", "avgo", "micron", "snowflake",
    "servicenow", "arm", "marvell", "oracle", "palantir", "datadog", "crowdstrike", "mongodb",
}
AI_EXPLICIT_TERMS = {
    "ai", "artificial intelligence", "generative ai", "genai", "large language model", "llm", "foundation model",
}
AI_MODEL_TERMS = {"chatbot", "copilot", "agent", "agents", "model training", "inference", "open-weight", "multimodal"}
AI_CHIP_TERMS = {"ai chip", "gpu", "accelerator", "hbm", "asic", "semiconductor", "chip demand", "advanced packaging"}
AI_INFRA_TERMS = {"data center", "datacenter", "cloud capex", "capital expenditure", "server rack", "training cluster", "power demand", "liquid cooling"}
AI_APP_TERMS = {"ai application", "enterprise ai", "saas", "automation", "robotics", "autonomous driving", "self-driving"}
MACRO_TERMS = {
    "fed", "federal reserve", "powell", "fomc", "rate cut", "interest rate", "treasury", "yield",
    "inflation", "cpi", "pce", "payroll", "jobs report", "gdp", "dollar", "oil", "crude", "gold",
}
CHINA_TERMS = {"china", "beijing", "hong kong", "tariff", "alibaba", "baba", "tencent", "jd.com", "pdd", "baidu", "bidu", "nio", "xpeng", "li auto"}
GEOPOLITICS_TERMS = {
    "ukraine", "russia", "israel", "iran", "gaza", "red sea", "sanction", "geopolitical", "war",
    "ceasefire", "export control", "export controls", "chip curbs", "senate", "congress", "testify",
    "hearing", "national security", "taiwan",
}

COMPANY_LEVEL_ALIASES = {
    "NVDA": {"nvidia", "nvda"},
    "AMD": {"advanced micro devices", "amd"},
    "AVGO": {"broadcom", "avgo"},
    "MSFT": {"microsoft", "msft", "azure"},
    "GOOGL": {"google", "alphabet", "googl", "gemini", "deepmind"},
    "META": {"meta platforms", "meta", "facebook", "instagram", "whatsapp"},
    "AMZN": {"amazon", "amzn", "aws"},
    "AAPL": {"apple", "aapl"},
    "TSLA": {"tesla", "tsla"},
    "MU": {"micron", "micron technology"},
    "MRVL": {"marvell", "marvell technology"},
    "ARM": {"arm holdings", "arm ltd", "arm"},
    "SNOW": {"snowflake"},
    "NOW": {"servicenow", "service now"},
    "PLTR": {"palantir"},
    "ORCL": {"oracle"},
    "CRWD": {"crowdstrike"},
    "DDOG": {"datadog"},
    "MDB": {"mongodb"},
}
AMBIGUOUS_LOWERCASE_TICKERS = {"now", "snow", "arm", "mu"}


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, IndexError):
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def entry_published_at(entry: Any) -> datetime | None:
    for key in ("published", "updated", "created"):
        value = getattr(entry, key, None) or entry.get(key)
        parsed = parse_datetime(value)
        if parsed:
            return parsed
    return None


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def phrase_present(normalized_text: str, term: str) -> bool:
    normalized_term = term.lower().strip()
    if not normalized_term:
        return False
    if normalized_term in AMBIGUOUS_LOWERCASE_TICKERS:
        return False
    if re.fullmatch(r"[a-z0-9.+-]+", normalized_term):
        pattern = rf"(?<![a-z0-9]){re.escape(normalized_term)}(?![a-z0-9])"
        return bool(re.search(pattern, normalized_text))
    return normalized_term in normalized_text


def raw_ticker_present(raw_text: str, ticker: str) -> bool:
    patterns = [rf"\${re.escape(ticker)}\b", rf"(?<![A-Z0-9]){re.escape(ticker)}(?![A-Z0-9])"]
    return any(re.search(pattern, raw_text) for pattern in patterns)


def term_hits(text: str, terms: set[str] | list[str]) -> list[str]:
    normalized = normalize_text(text)
    return [term for term in terms if phrase_present(normalized, term)]


def company_ticker_hits(title: str, summary: str, config: dict[str, Any]) -> dict[str, list[str]]:
    raw_text = f"{title} {summary}"
    normalized = normalize_text(raw_text)
    configured = {str(item.get("ticker", "")).upper() for item in config.get("market", {}).get("key_stocks", [])}
    hits: dict[str, list[str]] = {}
    for ticker, aliases in COMPANY_LEVEL_ALIASES.items():
        if configured and ticker not in configured:
            continue
        matched = [alias for alias in aliases if phrase_present(normalized, alias)]
        if raw_ticker_present(raw_text, ticker):
            matched.append(ticker)
        if matched:
            hits[ticker] = sorted(set(matched))
    return hits


def ai_relevance(text: str) -> tuple[int, list[str]]:
    company_hits = term_hits(text, AI_COMPANIES)
    explicit_hits = term_hits(text, AI_EXPLICIT_TERMS)
    model_hits = term_hits(text, AI_MODEL_TERMS)
    chip_hits = term_hits(text, AI_CHIP_TERMS)
    infra_hits = term_hits(text, AI_INFRA_TERMS)
    app_hits = term_hits(text, AI_APP_TERMS)
    topic_hits = sorted(set(explicit_hits + model_hits + chip_hits + infra_hits + app_hits))

    model_company_hits = {"openai", "anthropic", "gemini", "deepmind"}.intersection(set(company_hits))
    qualified = bool(model_company_hits)
    qualified = qualified or bool(explicit_hits and (company_hits or model_hits or chip_hits or infra_hits or app_hits))
    qualified = qualified or bool(company_hits and (model_hits or chip_hits or infra_hits or app_hits))
    if not qualified:
        return 0, []

    score = len(set(company_hits)) * 2 + len(set(explicit_hits)) * 3 + len(set(model_hits)) * 2
    score += len(set(chip_hits)) * 2 + len(set(infra_hits)) * 2 + len(set(app_hits))
    if model_company_hits:
        score += 3
    if company_hits and topic_hits:
        score += 2
    return score, sorted(set(company_hits + topic_hits))


def infer_tags(title: str, summary: str, config: dict[str, Any]) -> tuple[list[str], dict[str, int], dict[str, list[str]], list[str], list[str]]:
    text = f"{title} {summary}"
    tags: list[str] = []
    levels: list[str] = []
    scores: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}

    company_hits = company_ticker_hits(title, summary, config)
    if company_hits:
        levels.append("公司级")
        for ticker, matched in company_hits.items():
            tags.append(ticker)
            reasons[f"公司级:{ticker}"] = matched

    ai_score, ai_reasons = ai_relevance(text)
    if ai_score >= 5:
        tags.append("AI")
        levels.append("产业级")
        scores["AI"] = ai_score
        reasons["AI"] = ai_reasons

    macro_hits = term_hits(text, MACRO_TERMS)
    if macro_hits:
        tags.append("宏观")
        levels.append("宏观级")
        scores["宏观"] = len(set(macro_hits))
        reasons["宏观"] = sorted(set(macro_hits))

    china_hits = term_hits(text, CHINA_TERMS)
    if china_hits:
        tags.append("中概/AH")
        scores["中概/AH"] = len(set(china_hits))
        reasons["中概/AH"] = sorted(set(china_hits))

    geopolitics_hits = term_hits(text, GEOPOLITICS_TERMS)
    if geopolitics_hits:
        tags.append("地缘")
        levels.append("地缘级")
        scores["地缘"] = len(set(geopolitics_hits))
        reasons["地缘"] = sorted(set(geopolitics_hits))

    if "中概/AH" in tags and "地缘级" not in levels:
        levels.append("宏观级")
    if not levels:
        levels.append("未归类")
    return sorted(set(tags)), scores, reasons, sorted(set(levels)), sorted(company_hits.keys())


def fetch_feed(feed: dict[str, str], config: dict[str, Any], fetched_at: str) -> tuple[list[dict[str, Any]], str | None]:
    news_config = config.get("news", {})
    timeout = int(news_config.get("request_timeout_sec", 15))
    max_items = int(news_config.get("max_items_per_feed", 20))
    headers = {"User-Agent": news_config.get("user_agent", "us-market-review/0.1")}

    try:
        response = requests.get(feed["url"], timeout=timeout, headers=headers)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - continue with other feeds.
        return [], str(exc)

    parsed = feedparser.parse(response.content)
    items: list[dict[str, Any]] = []
    for entry in parsed.entries[:max_items]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        summary = (entry.get("summary") or entry.get("description") or "").strip()
        published = entry_published_at(entry)
        published_at = published.isoformat() if published else None
        tags, scores, reasons, levels, company_tickers = infer_tags(title, summary, config)
        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "source": feed.get("name", feed["url"]),
                "source_url": feed["url"],
                "published_at": published_at,
                "fetched_at": fetched_at,
                "tags": tags,
                "classification": levels,
                "company_tickers": company_tickers,
                "topic_scores": scores,
                "topic_reasons": reasons,
            }
        )
    return items, None


def fetch_news(config: dict[str, Any]) -> dict[str, Any]:
    timezone_name = config.get("app", {}).get("timezone", "Asia/Shanghai")
    fetched_at = now_iso(timezone_name)
    news_config = config.get("news", {})
    lookback_hours = int(news_config.get("lookback_hours", 36))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    all_items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen: set[str] = set()

    for feed in news_config.get("feeds", []):
        items, error = fetch_feed(feed, config, fetched_at)
        if error:
            errors.append({"source": feed.get("name", feed.get("url", "")), "error": error})
            continue

        for item in items:
            published = parse_datetime(item.get("published_at"))
            if published and published < cutoff:
                continue
            key = item.get("link") or item.get("title")
            if not key or key in seen:
                continue
            seen.add(key)
            all_items.append(item)

    all_items.sort(key=lambda item: item.get("published_at") or "", reverse=True)
    return {
        "metadata": {
            "source": "Public RSS feeds",
            "lookback_hours": lookback_hours,
            "fetched_at": fetched_at,
            "timezone": timezone_name,
            "tagging": "company-level strict match + industry/macro/geopolitical classification",
        },
        "items": all_items,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch RSS news and print a JSON snapshot.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    config = load_config(args.config)
    news_data = fetch_news(config)
    payload = json.dumps(news_data, ensure_ascii=False, indent=2)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(payload, encoding="utf-8")
    else:
        print(payload)


if __name__ == "__main__":
    main()
