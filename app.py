"""A simple Streamlit dashboard for the AI Morning Brief."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

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


def initialize_reader_state() -> None:
    """Create private, temporary storage for one visitor's browser tab."""
    st.session_state.setdefault("collected_articles", {})
    st.session_state.setdefault("article_notes", {})


def article_key(article: dict[str, Any]) -> str:
    """Build a short, stable key for Streamlit widgets and saved notes."""
    identity = "|".join(
        [
            str(article.get("url") or ""),
            str(article.get("title") or ""),
            str(article.get("date") or ""),
        ]
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def toggle_collection(article: dict[str, Any], key: str) -> None:
    """Add or remove an article from this visitor's reading desk."""
    collected = st.session_state["collected_articles"]
    if key in collected:
        del collected[key]
        st.session_state["reader_message"] = "Removed from your collection."
    else:
        # Store a copy so the collected view remains stable after filtering.
        collected[key] = dict(article)
        st.session_state["reader_message"] = "Added to your collection."


def save_note(key: str, widget_key: str) -> None:
    """Save or remove a personal note from this browser session."""
    note = str(st.session_state.get(widget_key, "")).strip()
    notes = st.session_state["article_notes"]
    if note:
        notes[key] = note
        st.session_state["reader_message"] = "Your note was saved."
    else:
        notes.pop(key, None)
        st.session_state["reader_message"] = "The empty note was removed."


def clear_collection() -> None:
    """Clear collected articles while leaving personal notes untouched."""
    st.session_state["collected_articles"].clear()
    st.session_state["reader_message"] = "Your collection was cleared."


def collection_as_markdown() -> str:
    """Create a portable Markdown file from collected articles and notes."""
    collected = st.session_state["collected_articles"]
    notes = st.session_state["article_notes"]
    lines = ["# My AI reading collection", ""]

    for key, article in collected.items():
        title = str(article.get("title") or "Untitled article")
        source = str(article.get("source") or "Unknown source")
        published_date = parse_date(article.get("date"))
        date_text = published_date.isoformat() if published_date else "Unknown date"
        url = str(article.get("url") or "")
        summary = str(article.get("summary") or "No summary available.")

        lines.extend(
            [
                f"## {title}",
                "",
                f"**Source:** {source} · **Date:** {date_text}",
                "",
                summary,
                "",
            ]
        )
        if url:
            lines.extend([f"[Read the original article]({url})", ""])
        if note := notes.get(key):
            lines.extend([f"**My note:** {note}", ""])

    return "\n".join(lines)


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


def source_badge_color(source: str) -> str:
    """Give each recurring publisher a consistent newspaper accent."""
    color_by_source = {
        "OpenAI": "green",
        "Google AI": "blue",
        "MIT Technology Review": "orange",
        "VentureBeat AI": "violet",
    }
    return color_by_source.get(source, "gray")


def display_article_card(article: dict[str, Any]) -> None:
    """Render one newspaper-style article card with reader actions."""
    title = str(article.get("title") or "Untitled article")
    source = str(article.get("source") or "Unknown source")
    published_date = parse_date(article.get("date"))
    date_text = published_date.isoformat() if published_date else "Unknown date"
    summary = str(article.get("summary") or "No summary available.")
    url = str(article.get("url") or "")
    key = article_key(article)
    collected = key in st.session_state["collected_articles"]
    note_widget_key = f"note_text_{key}"
    st.session_state.setdefault(
        note_widget_key,
        st.session_state["article_notes"].get(key, ""),
    )

    with st.container(border=True, gap="small"):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.badge(source, color=source_badge_color(source))
            st.caption(date_text)
        st.subheader(title)
        st.write(summary)

        with st.container(
            horizontal=True,
            vertical_alignment="center",
            gap="small",
        ):
            if url:
                st.link_button(
                    "Read original article",
                    url,
                    type="primary",
                    icon=":material/open_in_new:",
                )

            st.button(
                "Collected" if collected else "Collect",
                key=f"collect_{key}",
                icon=":material/bookmark:" if collected else ":material/bookmark_add:",
                on_click=toggle_collection,
                args=(article, key),
            )

            with st.popover(
                "Share",
                key=f"share_{key}",
                icon=":material/share:",
            ):
                if url:
                    st.caption("Copy this link or share it by email.")
                    st.code(url, language=None, wrap_lines=True)
                    email_subject = quote(title)
                    email_body = quote(f"{title}\n\n{url}")
                    email_url = (
                        f"mailto:?subject={email_subject}"
                        f"&body={email_body}"
                    )
                    st.link_button(
                        "Share by email",
                        email_url,
                        icon=":material/mail:",
                    )
                else:
                    st.caption("This article does not have a shareable URL.")

            note_label = (
                "Edit note"
                if key in st.session_state["article_notes"]
                else "Note"
            )
            with st.popover(
                note_label,
                key=f"note_{key}",
                icon=":material/edit_note:",
            ):
                st.text_area(
                    "My learning note",
                    key=note_widget_key,
                    placeholder="Write a takeaway, new word, or question...",
                    height=120,
                    persist_state="session",
                )
                st.button(
                    "Save note",
                    key=f"save_note_{key}",
                    type="primary",
                    icon=":material/save:",
                    on_click=save_note,
                    args=(key, note_widget_key),
                )

        saved_note = st.session_state["article_notes"].get(key)
        if saved_note:
            st.caption(f"My note: {saved_note}")


def main() -> None:
    """Build the Streamlit page."""
    st.set_page_config(
        page_title="The AI Morning Brief",
        page_icon=":material/newspaper:",
        layout="centered",
    )
    initialize_reader_state()

    st.caption(
        "DAILY AI INTELLIGENCE · RSS EDITION",
        text_alignment="center",
    )
    st.title("The AI Morning Brief", text_alignment="center")
    st.caption(
        "A concise newspaper for learning AI, industry trends, and English.",
        text_alignment="center",
    )

    if message := st.session_state.pop("reader_message", None):
        st.toast(message, icon=":material/check_circle:")

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

    st.subheader("Today's front page")
    update_columns = st.columns(3, vertical_alignment="center")
    with update_columns[0].container(border=True):
        st.metric("Last update", last_update)
    with update_columns[1].container(border=True):
        st.metric("Briefing articles", current_stats["briefing"])
    with update_columns[2].container(border=True):
        st.metric("Archive", current_stats["total"])
    st.caption(
        f"Edition window: {briefing_start:%Y-%m-%d %H:%M} to "
        f"{briefing_end:%Y-%m-%d %H:%M} (Asia/Shanghai)"
    )
    st.caption("A fresh edition is prepared automatically every day at 08:00.")

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
        options=["Morning briefing", "All history", "Collected"],
        default="Morning briefing",
        required=True,
        key="news_view",
    )
    if news_view == "Collected":
        view_articles = list(
            st.session_state["collected_articles"].values()
        )
        view_key = "collected"
    elif news_view == "All history":
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
        if news_view == "Collected":
            st.info(
                "Your reading desk is empty. Select Collect beside any article "
                "to save it for this browser session.",
                icon=":material/bookmark_add:",
            )
        else:
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

    collected_count = len(st.session_state["collected_articles"])
    st.sidebar.header("Your reading desk")
    st.sidebar.metric("Collected articles", collected_count)
    if collected_count:
        st.sidebar.download_button(
            "Download collection",
            data=collection_as_markdown(),
            file_name=f"my_ai_reading_{date.today().isoformat()}.md",
            mime="text/markdown",
            icon=":material/download:",
            on_click="ignore",
        )
        st.sidebar.button(
            "Clear collection",
            icon=":material/delete_sweep:",
            on_click=clear_collection,
        )
    st.sidebar.caption(
        "Collections and notes are private to this browser tab. "
        "Download your collection before closing it."
    )

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

    st.subheader("Top stories")
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
