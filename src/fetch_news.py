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
    "openai",
    "anthropic",
    "google",
    "alphabet",
    "meta",
    "microsoft",
    "nvidia",
    "nvda",
    "amd",
    "broadcom",
    "avgo",
    "micron",
    "mu",
    "snowflake",
    "servicenow",
    "arm",
    "marvell",
    "mrvl",
}
AI_TOPICS = {
    "artificial intelligence",
    "generative ai",
    "large language model",
    "llm",
    "ai chip",
    "gpu",
    "accelerator",
    "semiconductor",
    "data center",
    "datacenter",
    "cloud capex",
    "capex",
    "inference",
    "training cluster",
    "robotics",
    "autonomous driving",
    "self-driving",
    "saas",
}
MACRO_TERMS = {
    "fed",
    "federal reserve",
    "powell",
    "fomc",
    "rate cut",
    "interest rate",
    "treasury",
    "yield",
    "inflation",
    "cpi",
    "pce",
    "payroll",
    "jobs report",
    "gdp",
    "dollar",
    "oil",
    "crude",
    "gold",
}
CHINA_TERMS = {
    "china",
    "beijing",
    "hong kong",
    "tariff",
    "alibaba",
    "baba",
    "tencent",
    "jd.com",
    "jd",
    "pdd",
    "baidu",
    "bidu",
    "nio",
    "xpeng",
    "li auto",
}
GEOPOLITICS_TERMS = {
    "ukraine",
    "russia",
    "israel",
    "iran",
    "gaza",
    "red sea",
    "sanction",
    "geopolitical",
    "war",
    "ceasefire",
}


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


def term_hits(text: str, terms: set[str] | list[str]) -> list[str]:
    normalized = normalize_text(text)
    return [term for term in terms if term.lower() in normalized]


def ai_relevance(text: str) -> tuple[int, list[str]]:
    company_hits = term_hits(text, AI_COMPANIES)
    topic_hits = term_hits(text, AI_TOPICS)
    explicit_ai = bool(re.search(r"\bai\b|artificial intelligence|generative ai|large language model|\bllm\b", normalize_text(text)))
    score = len(set(company_hits)) + len(set(topic_hits))
    if explicit_ai:
        score += 2
    if company_hits and topic_hits:
        score += 2
    reasons = sorted(set(company_hits + topic_hits))
    return score, reasons


def infer_tags(title: str, summary: str, config: dict[str, Any]) -> tuple[list[str], dict[str, int], dict[str, list[str]]]:
    text = f"{title} {summary}"
    tags: list[str] = []
    scores: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}

    ai_score, ai_reasons = ai_relevance(text)
    if ai_score >= 4:
        tags.append("AI")
        scores["AI"] = ai_score
        reasons["AI"] = ai_reasons

    macro_hits = term_hits(text, MACRO_TERMS)
    if len(set(macro_hits)) >= 1:
        tags.append("宏观")
        scores["宏观"] = len(set(macro_hits))
        reasons["宏观"] = sorted(set(macro_hits))

    china_hits = term_hits(text, CHINA_TERMS)
    if len(set(china_hits)) >= 1:
        tags.append("中概/AH")
        scores["中概/AH"] = len(set(china_hits))
        reasons["中概/AH"] = sorted(set(china_hits))

    geopolitics_hits = term_hits(text, GEOPOLITICS_TERMS)
    if len(set(geopolitics_hits)) >= 1:
        tags.append("地缘")
        scores["地缘"] = len(set(geopolitics_hits))
        reasons["地缘"] = sorted(set(geopolitics_hits))

    for stock in config.get("market", {}).get("key_stocks", []):
        ticker = str(stock.get("ticker", ""))
        name = str(stock.get("name", ""))
        company_terms = {ticker.lower(), name.lower()} - {""}
        if term_hits(text, company_terms):
            tags.append(ticker)

    return sorted(set(tags)), scores, reasons


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
        tags, scores, reasons = infer_tags(title, summary, config)
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
            "tagging": "score-based topical relevance",
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
