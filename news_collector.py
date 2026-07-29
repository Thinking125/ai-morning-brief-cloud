"""Collect and normalize AI news from RSS feeds."""

from __future__ import annotations

import calendar
import html
import re
from datetime import datetime, timezone
from typing import Any

import feedparser


def clean_html(text: str) -> str:
    """Turn the small HTML snippets found in RSS feeds into plain text."""
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    plain_text = html.unescape(without_tags)
    return re.sub(r"\s+", " ", plain_text).strip()


def entry_date(entry: Any) -> datetime | None:
    """Read a feed date and return a timezone-aware UTC datetime."""
    date_tuple = entry.get("published_parsed") or entry.get("updated_parsed")
    if not date_tuple:
        return None
    return datetime.fromtimestamp(calendar.timegm(date_tuple), tz=timezone.utc)


def collect_news(
    feeds: list[dict[str, str]], max_articles_per_feed: int
) -> list[dict[str, Any]]:
    """Download each RSS feed and return articles in one consistent format."""
    articles: list[dict[str, Any]] = []

    for feed_config in feeds:
        source = feed_config["name"]
        parsed_feed = None
        last_error: Exception | None = None

        # RSS servers occasionally close a connection early. One immediate retry
        # is enough for this prototype and avoids adding a retry library.
        for _ in range(2):
            try:
                candidate = feedparser.parse(
                    feed_config["url"],
                    request_headers={"User-Agent": "AI-Morning-Brief/0.1"},
                )
                parsed_feed = candidate
                if candidate.entries or not candidate.bozo:
                    break
            except Exception as error:
                last_error = error

        if parsed_feed is None:
            # Skip only this source so the remaining feeds can still succeed.
            print(f"Warning: could not download {source}: {last_error}")
            continue

        if parsed_feed.bozo:
            # A broken feed should not stop the other feeds from working.
            print(f"Warning: could not fully read {source}: {parsed_feed.bozo_exception}")

        for entry in parsed_feed.entries[:max_articles_per_feed]:
            summary = entry.get("summary") or entry.get("description") or ""
            articles.append(
                {
                    "title": clean_html(entry.get("title", "Untitled article")),
                    "source": source,
                    "date": entry_date(entry),
                    "url": entry.get("link", ""),
                    "summary": clean_html(summary),
                }
            )

    return articles
