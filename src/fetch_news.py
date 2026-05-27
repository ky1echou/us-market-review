from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import feedparser
import requests

from .fetch_market import load_config, now_iso


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


def text_contains_any(text: str, keywords: list[str]) -> list[str]:
    lower_text = text.lower()
    return [keyword for keyword in keywords if keyword.lower() in lower_text]


def infer_tags(title: str, summary: str, config: dict[str, Any]) -> list[str]:
    themes = config.get("themes", {})
    text = f"{title} {summary}"
    tags: list[str] = []

    if text_contains_any(text, themes.get("ai_keywords", [])):
        tags.append("AI")
    if text_contains_any(text, themes.get("macro_keywords", [])):
        tags.append("宏观")
    if text_contains_any(text, themes.get("china_keywords", [])):
        tags.append("中概/AH")

    for stock in config.get("market", {}).get("key_stocks", []):
        ticker = str(stock.get("ticker", ""))
        name = str(stock.get("name", ""))
        if ticker and text_contains_any(text, [ticker]):
            tags.append(ticker)
        elif name and text_contains_any(text, [name]):
            tags.append(ticker)

    return sorted(set(tags))


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
        items.append(
            {
                "title": title,
                "link": link,
                "summary": summary,
                "source": feed.get("name", feed["url"]),
                "source_url": feed["url"],
                "published_at": published_at,
                "fetched_at": fetched_at,
                "tags": infer_tags(title, summary, config),
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
