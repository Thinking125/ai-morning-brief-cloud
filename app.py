"""A simple Streamlit dashboard for the AI Morning Brief."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

import streamlit as st

from database import (
    DEFAULT_DB_PATH,
    REPORT_TIMEZONE,
    article_stats,
    current_briefing_window,
    database_signature,
    initialize_database,
    load_dashboard_articles,
    parse_datetime,
    sync_articles_from_json,
)


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_DIR / "output"


def find_articles_file() -> Path | None:
    """Find articles.json, or fall back to the newest dated article file."""
    preferred_file = OUTPUT_DIR / "articles.json"
    if preferred_file.exists():
        return preferred_file

    dated_files = list(OUTPUT_DIR.glob("*_articles.json"))
    if not dated_files:
        return None

    # max(..., key=...) returns the most recently modified file.
    return max(dated_files, key=lambda path: path.stat().st_mtime)


@st.cache_data(max_entries=10)
def load_articles(
    database_path: str,
    modified_signature: tuple[int, int],
) -> list[dict[str, Any]]:
    """Read the current RSS article records from SQLite.

    modified_signature makes Streamlit reload when SQLite or its WAL changes.
    """
    del modified_signature
    return load_dashboard_articles(Path(database_path))


@st.cache_resource(max_entries=5)
def prepare_database(
    database_path: str,
    article_store_path: str | None,
    article_store_modified: int,
) -> None:
    """Build or refresh the cloud's temporary SQLite copy from permanent JSON."""
    del article_store_modified
    database = Path(database_path)
    initialize_database(database)
    if article_store_path:
        sync_articles_from_json(Path(article_store_path), database)


def parse_date(value: Any) -> date | None:
    """Convert an ISO timestamp into an Asia/Shanghai calendar date."""
    parsed = parse_datetime(str(value)) if value else None
    return parsed.astimezone(REPORT_TIMEZONE).date() if parsed else None


def article_is_in_briefing_window(
    article: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
) -> bool:
    """Return True when an article belongs to the 08:00-to-08:00 brief."""
    published_at = parse_datetime(article.get("date"))
    first_seen_at = parse_datetime(article.get("first_seen_at"))
    effective_time = published_at or first_seen_at
    if effective_time is None:
        return False

    local_time = effective_time.astimezone(REPORT_TIMEZONE)
    return window_start <= local_time < window_end


def article_matches_keyword(article: dict[str, Any], keyword: str) -> bool:
    """Search the title, source, and summary without case sensitivity."""
    if not keyword:
        return True

    searchable_text = " ".join(
        [
            str(article.get("title", "")),
            str(article.get("source", "")),
            str(article.get("summary", "")),
        ]
    )
    return keyword.casefold() in searchable_text.casefold()


def filter_articles(
    articles: list[dict[str, Any]],
    selected_sources: list[str],
    start_date: date | None,
    end_date: date | None,
    keyword: str,
) -> list[dict[str, Any]]:
    """Apply all dashboard filters to the article list."""
    filtered_articles: list[dict[str, Any]] = []

    for article in articles:
        source = str(article.get("source", "Unknown source"))
        published_date = parse_date(article.get("date"))

        if source not in selected_sources:
            continue
        if start_date and (published_date is None or published_date < start_date):
            continue
        if end_date and (published_date is None or published_date > end_date):
            continue
        if not article_matches_keyword(article, keyword):
            continue

        filtered_articles.append(article)

    # Show the newest articles first. Missing or invalid dates go last.
    return sorted(
        filtered_articles,
        key=lambda article: parse_date(article.get("date")) or date.min,
        reverse=True,
    )


def display_article_card(article: dict[str, Any]) -> None:
    """Render one article as a bordered Streamlit card."""
    title = str(article.get("title") or "Untitled article")
    source = str(article.get("source") or "Unknown source")
    published_date = parse_date(article.get("date"))
    date_text = published_date.isoformat() if published_date else "Unknown date"
    summary = str(article.get("summary") or "No summary available.")
    url = str(article.get("url") or "")

    with st.container(border=True):
        st.subheader(title)
        st.caption(f"{source} · {date_text}")
        st.write(summary)

        if url:
            st.link_button("Read original article", url)
        else:
            st.caption("No article URL is available.")


def main() -> None:
    """Build the Streamlit page."""
    st.set_page_config(
        page_title="AI Morning Brief",
        page_icon="🧠",
        layout="wide",
    )

    st.title("🧠 AI Morning Brief")
    st.write("Browse and filter the latest collected AI news.")

    articles_file = find_articles_file()
    articles: list[dict[str, Any]] = []
    load_error: str | None = None
    fallback_start, fallback_end = current_briefing_window()

    try:
        prepare_database(
            str(DEFAULT_DB_PATH),
            str(articles_file) if articles_file else None,
            articles_file.stat().st_mtime_ns if articles_file else 0,
        )

        articles = load_articles(
            str(DEFAULT_DB_PATH),
            database_signature(DEFAULT_DB_PATH),
        )
        current_stats = article_stats(path=DEFAULT_DB_PATH)
    except (OSError, sqlite3.DatabaseError, ValueError) as error:
        load_error = f"Could not read the SQLite database: {error}"
        current_stats = {
            "briefing": 0,
            "total": 0,
            "briefing_start": fallback_start,
            "briefing_end": fallback_end,
            "last_update": None,
        }

    last_update_value = current_stats["last_update"]
    last_update = (
        last_update_value.strftime("%Y-%m-%d %H:%M")
        if last_update_value
        else "Never"
    )

    briefing_start = current_stats["briefing_start"]
    briefing_end = current_stats["briefing_end"]

    st.subheader("Today's briefing")
    update_columns = st.columns(3, vertical_alignment="center")
    update_columns[0].metric("Last update", last_update)
    update_columns[1].metric(
        "Briefing articles (24h)",
        current_stats["briefing"],
    )
    update_columns[2].metric("Total articles", current_stats["total"])
    st.caption(
        f"Window: {briefing_start:%Y-%m-%d %H:%M} to "
        f"{briefing_end:%Y-%m-%d %H:%M} (Asia/Shanghai)"
    )
    st.caption("Cloud data updates automatically every day at 08:00.")

    if not articles and not load_error:
        st.error("No article data was found in the cloud article store.")
        st.stop()

    if load_error:
        st.error(load_error)
        st.stop()

    if not articles:
        st.warning(f"{articles_file.name} does not contain any articles.")
        st.stop()

    news_view = st.segmented_control(
        "News view",
        options=["Morning briefing", "All history"],
        default="Morning briefing",
        required=True,
        key="news_view",
    )
    if news_view == "All history":
        view_articles = articles
        view_key = "history"
    else:
        view_articles = [
            article
            for article in articles
            if article_is_in_briefing_window(
                article,
                briefing_start,
                briefing_end,
            )
        ]
        view_key = "briefing"

    if not view_articles:
        st.info(
            "No articles were found in this briefing window. "
            "Choose All history to browse older articles."
        )
        return

    sources = sorted(
        {
            str(article.get("source") or "Unknown source")
            for article in view_articles
        }
    )
    available_dates = [
        parsed_date
        for article in view_articles
        if (parsed_date := parse_date(article.get("date"))) is not None
    ]

    st.sidebar.header("Filters")
    selected_sources = st.sidebar.multiselect(
        "Source",
        options=sources,
        default=sources,
        key=f"source_filter_{view_key}",
    )
    keyword = st.sidebar.text_input(
        "Keyword search",
        placeholder="Try: model, agent, OpenAI...",
        key=f"keyword_filter_{view_key}",
    )

    start_date: date | None = None
    end_date: date | None = None
    if available_dates:
        selected_dates = st.sidebar.date_input(
            "Date range",
            value=(min(available_dates), max(available_dates)),
            min_value=min(available_dates),
            max_value=max(available_dates),
            key=f"date_filter_{view_key}",
        )

        # During date selection, Streamlit may temporarily return one date.
        if isinstance(selected_dates, tuple):
            if len(selected_dates) >= 1:
                start_date = selected_dates[0]
            if len(selected_dates) == 2:
                end_date = selected_dates[1]
        else:
            start_date = end_date = selected_dates
    else:
        st.sidebar.caption("No valid article dates are available.")

    filtered_articles = filter_articles(
        view_articles,
        selected_sources,
        start_date,
        end_date,
        keyword.strip(),
    )

    st.caption(
        f"Showing {len(filtered_articles)} of {len(view_articles)} articles "
        f"in {news_view.lower()}"
    )

    if not filtered_articles:
        st.info("No articles match the selected filters.")
        return

    for article in filtered_articles:
        display_article_card(article)


if __name__ == "__main__":
    main()
