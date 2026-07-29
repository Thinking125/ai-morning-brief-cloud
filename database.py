"""SQLite storage helpers for articles and AI analysis."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
DEFAULT_DB_PATH = DATA_DIR / "ai_morning_brief.db"
REPORT_TIMEZONE = ZoneInfo("Asia/Shanghai")
BRIEFING_HOUR = 8


SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    canonical_url TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL,
    source TEXT NOT NULL,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_versions (
    id INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    rss_summary TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE,
    UNIQUE (article_id, content_hash)
);

CREATE TABLE IF NOT EXISTS article_analysis (
    id INTEGER PRIMARY KEY,
    article_version_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    importance_score INTEGER NOT NULL CHECK (importance_score BETWEEN 1 AND 5),
    category TEXT NOT NULL,
    summary TEXT NOT NULL,
    why_it_matters TEXT NOT NULL,
    technical_explanation TEXT NOT NULL,
    personal_relevance TEXT NOT NULL,
    vocabulary_json TEXT NOT NULL,
    analyzed_at TEXT NOT NULL,
    FOREIGN KEY (article_version_id) REFERENCES article_versions(id)
        ON DELETE CASCADE,
    UNIQUE (article_version_id, provider, model, prompt_version)
);

CREATE TABLE IF NOT EXISTS analysis_attempts (
    id INTEGER PRIMARY KEY,
    article_version_id INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    error_message TEXT,
    attempted_at TEXT NOT NULL,
    FOREIGN KEY (article_version_id) REFERENCES article_versions(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_articles_published_at
    ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_article_versions_article
    ON article_versions(article_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_version
    ON article_analysis(article_version_id);
"""


def connect_database(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open SQLite with safe defaults for this local application."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


@contextmanager
def database_connection(path: Path = DEFAULT_DB_PATH):
    """Yield a connection, then commit or roll back and always close it."""
    connection = connect_database(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(path: Path = DEFAULT_DB_PATH) -> None:
    """Create the database tables when they do not yet exist."""
    with database_connection(path) as connection:
        connection.executescript(SCHEMA)


def normalize_url(url: str, source: str, title: str) -> str:
    """Return a stable duplicate key for an RSS article."""
    cleaned_url = url.strip().rstrip("/")
    if cleaned_url:
        return cleaned_url.casefold()

    fallback = f"{source.strip().casefold()}|{title.strip().casefold()}"
    return "urn:article:" + hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def article_content_hash(title: str, summary: str) -> str:
    """Detect meaningful title or summary changes."""
    content = f"{title.strip()}\n{summary.strip()}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def utc_now_text() -> str:
    """Return the current UTC time in an SQLite-friendly ISO format."""
    return datetime.now(timezone.utc).isoformat()


def sync_articles(
    articles: list[dict[str, Any]],
    path: Path = DEFAULT_DB_PATH,
) -> tuple[int, int]:
    """Insert JSON-style articles into SQLite without overwriting old versions."""
    initialize_database(path)
    new_articles = 0
    new_versions = 0
    observed_at = utc_now_text()

    with database_connection(path) as connection:
        for article in articles:
            title = str(article.get("title") or "").strip()
            source = str(article.get("source") or "Unknown source").strip()
            url = str(article.get("url") or "").strip()
            summary = str(article.get("summary") or "").strip()
            published_at = article.get("date")
            canonical_url = normalize_url(url, source, title)
            content_hash = article_content_hash(title, summary)

            existing = connection.execute(
                "SELECT id FROM articles WHERE canonical_url = ?",
                (canonical_url,),
            ).fetchone()

            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO articles (
                        canonical_url, url, source, published_at,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        canonical_url,
                        url,
                        source,
                        published_at,
                        observed_at,
                        observed_at,
                    ),
                )
                article_id = int(cursor.lastrowid)
                new_articles += 1
            else:
                article_id = int(existing["id"])
                # Identity is stable; only current metadata and last-seen time change.
                connection.execute(
                    """
                    UPDATE articles
                    SET url = ?, source = ?,
                        published_at = COALESCE(?, published_at),
                        last_seen_at = ?
                    WHERE id = ?
                    """,
                    (url, source, published_at, observed_at, article_id),
                )

            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO article_versions (
                    article_id, content_hash, title, rss_summary, observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (article_id, content_hash, title, summary, observed_at),
            )
            if cursor.rowcount == 1:
                new_versions += 1

    return new_articles, new_versions


def sync_articles_from_json(
    json_path: Path,
    path: Path = DEFAULT_DB_PATH,
) -> tuple[int, int]:
    """Migrate the existing permanent JSON article store into SQLite."""
    if not json_path.exists():
        initialize_database(path)
        return 0, 0

    with json_path.open("r", encoding="utf-8") as article_file:
        articles = json.load(article_file)
    if not isinstance(articles, list):
        raise ValueError(f"{json_path.name} must contain a JSON list.")
    return sync_articles(articles, path)


def pending_article_versions(
    provider: str,
    model: str,
    prompt_version: str,
    limit: int,
    path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Return newest article versions that do not have matching AI analysis."""
    initialize_database(path)
    query = """
    WITH latest_versions AS (
        SELECT av.*
        FROM article_versions av
        WHERE av.id = (
            SELECT av2.id
            FROM article_versions av2
            WHERE av2.article_id = av.article_id
            ORDER BY av2.id DESC
            LIMIT 1
        )
    )
    SELECT
        a.id AS article_id,
        a.url,
        a.source,
        a.published_at AS date,
        lv.id AS article_version_id,
        lv.content_hash,
        lv.title,
        lv.rss_summary AS summary
    FROM articles a
    JOIN latest_versions lv ON lv.article_id = a.id
    LEFT JOIN article_analysis aa
        ON aa.article_version_id = lv.id
       AND aa.provider = ?
       AND aa.model = ?
       AND aa.prompt_version = ?
    WHERE aa.id IS NULL
    ORDER BY COALESCE(a.published_at, a.first_seen_at) DESC
    LIMIT ?
    """
    with database_connection(path) as connection:
        rows = connection.execute(
            query,
            (provider, model, prompt_version, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def save_analysis(
    article_version_id: int,
    provider: str,
    model: str,
    prompt_version: str,
    result: dict[str, Any],
    path: Path = DEFAULT_DB_PATH,
) -> None:
    """Save one validated analysis result and its successful attempt."""
    analyzed_at = utc_now_text()
    vocabulary_json = json.dumps(
        result["english_vocabulary"],
        ensure_ascii=False,
    )

    with database_connection(path) as connection:
        connection.execute(
            """
            INSERT INTO article_analysis (
                article_version_id, provider, model, prompt_version,
                importance_score, category, summary, why_it_matters,
                technical_explanation, personal_relevance,
                vocabulary_json, analyzed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(article_version_id, provider, model, prompt_version)
            DO UPDATE SET
                importance_score = excluded.importance_score,
                category = excluded.category,
                summary = excluded.summary,
                why_it_matters = excluded.why_it_matters,
                technical_explanation = excluded.technical_explanation,
                personal_relevance = excluded.personal_relevance,
                vocabulary_json = excluded.vocabulary_json,
                analyzed_at = excluded.analyzed_at
            """,
            (
                article_version_id,
                provider,
                model,
                prompt_version,
                result["importance_score"],
                result["category"],
                result["summary"],
                result["why_it_matters"],
                result["technical_explanation"],
                result["personal_relevance"],
                vocabulary_json,
                analyzed_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO analysis_attempts (
                article_version_id, provider, model, prompt_version,
                status, error_message, attempted_at
            ) VALUES (?, ?, ?, ?, 'success', NULL, ?)
            """,
            (
                article_version_id,
                provider,
                model,
                prompt_version,
                analyzed_at,
            ),
        )


def save_analysis_failure(
    article_version_id: int,
    provider: str,
    model: str,
    prompt_version: str,
    error_message: str,
    path: Path = DEFAULT_DB_PATH,
) -> None:
    """Record a failed article without exposing API secrets."""
    with database_connection(path) as connection:
        connection.execute(
            """
            INSERT INTO analysis_attempts (
                article_version_id, provider, model, prompt_version,
                status, error_message, attempted_at
            ) VALUES (?, ?, ?, ?, 'failed', ?, ?)
            """,
            (
                article_version_id,
                provider,
                model,
                prompt_version,
                error_message[:1000],
                utc_now_text(),
            ),
        )


def load_dashboard_articles(
    path: Path = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """Load current article versions and their newest saved AI analysis."""
    initialize_database(path)
    query = """
    WITH latest_versions AS (
        SELECT av.*
        FROM article_versions av
        WHERE av.id = (
            SELECT av2.id
            FROM article_versions av2
            WHERE av2.article_id = av.article_id
            ORDER BY av2.id DESC
            LIMIT 1
        )
    ),
    latest_analysis AS (
        SELECT aa.*
        FROM article_analysis aa
        WHERE aa.id = (
            SELECT aa2.id
            FROM article_analysis aa2
            WHERE aa2.article_version_id = aa.article_version_id
            ORDER BY aa2.analyzed_at DESC, aa2.id DESC
            LIMIT 1
        )
    )
    SELECT
        a.id,
        a.url,
        a.source,
        a.published_at AS date,
        a.first_seen_at,
        a.last_seen_at,
        lv.title,
        lv.rss_summary,
        la.importance_score,
        la.category,
        la.summary AS ai_summary,
        la.why_it_matters,
        la.technical_explanation,
        la.personal_relevance,
        la.vocabulary_json,
        la.analyzed_at
    FROM articles a
    JOIN latest_versions lv ON lv.article_id = a.id
    LEFT JOIN latest_analysis la ON la.article_version_id = lv.id
    ORDER BY COALESCE(a.published_at, a.first_seen_at) DESC
    """
    with database_connection(path) as connection:
        rows = connection.execute(query).fetchall()

    articles: list[dict[str, Any]] = []
    for row in rows:
        article = dict(row)
        article["summary"] = article.pop("rss_summary")
        vocabulary = article.pop("vocabulary_json")
        article["english_vocabulary"] = (
            json.loads(vocabulary) if vocabulary else []
        )
        articles.append(article)
    return articles


def parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO timestamp and ensure it has timezone information."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def current_briefing_window(
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return the most recent completed 08:00-to-08:00 Shanghai window."""
    local_now = now or datetime.now(REPORT_TIMEZONE)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=REPORT_TIMEZONE)
    else:
        local_now = local_now.astimezone(REPORT_TIMEZONE)

    window_end = datetime.combine(
        local_now.date(),
        time(hour=BRIEFING_HOUR),
        tzinfo=REPORT_TIMEZONE,
    )
    if local_now < window_end:
        window_end -= timedelta(days=1)

    window_start = window_end - timedelta(days=1)
    return window_start, window_end


def article_stats(
    report_date: date | None = None,
    now: datetime | None = None,
    path: Path = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """Return briefing-window and historical counts for the dashboard."""
    if report_date is None:
        window_start, window_end = current_briefing_window(now)
    else:
        window_end = datetime.combine(
            report_date,
            time(hour=BRIEFING_HOUR),
            tzinfo=REPORT_TIMEZONE,
        )
        window_start = window_end - timedelta(days=1)

    initialize_database(path)

    with database_connection(path) as connection:
        rows = connection.execute(
            "SELECT published_at, first_seen_at, last_seen_at FROM articles"
        ).fetchall()

    briefing_count = 0
    latest_update: datetime | None = None
    for row in rows:
        effective_date = parse_datetime(row["published_at"]) or parse_datetime(
            row["first_seen_at"]
        )
        if effective_date:
            local_date = effective_date.astimezone(REPORT_TIMEZONE)
            if window_start <= local_date < window_end:
                briefing_count += 1

        last_seen = parse_datetime(row["last_seen_at"])
        if last_seen and (latest_update is None or last_seen > latest_update):
            latest_update = last_seen

    return {
        "briefing": briefing_count,
        "total": len(rows),
        "briefing_start": window_start,
        "briefing_end": window_end,
        "last_update": (
            latest_update.astimezone(REPORT_TIMEZONE)
            if latest_update
            else None
        ),
    }


def database_signature(path: Path = DEFAULT_DB_PATH) -> tuple[int, int]:
    """Return DB and WAL modification times for Streamlit cache invalidation."""
    database_time = path.stat().st_mtime_ns if path.exists() else 0
    wal_path = Path(str(path) + "-wal")
    wal_time = wal_path.stat().st_mtime_ns if wal_path.exists() else 0
    return database_time, wal_time


if __name__ == "__main__":
    source_path = PROJECT_DIR / "output" / "articles.json"
    added_articles, added_versions = sync_articles_from_json(source_path)
    stats = article_stats()
    print(f"Database: {DEFAULT_DB_PATH}")
    print(f"New articles imported: {added_articles}")
    print(f"New article versions imported: {added_versions}")
    print(f"Total articles: {stats['total']}")
