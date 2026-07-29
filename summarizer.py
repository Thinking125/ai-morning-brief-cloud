"""Filter collected articles and generate the AI Morning Brief."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from database import (
    DEFAULT_DB_PATH,
    REPORT_TIMEZONE,
    current_briefing_window,
    sync_articles_from_json,
)
from news_collector import collect_news


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_DIR / "config.yaml"


VOCABULARY = {
    "agent": "agent — software that can plan and take actions toward a goal",
    "benchmark": "benchmark — a standard test used to compare systems",
    "funding": "funding — money provided to develop or grow a company",
    "inference": "inference — the process of using a trained model to produce an answer",
    "model": "model — a learned system that finds patterns and makes predictions",
    "multimodal": "multimodal — able to work with several data types, such as text and images",
    "open source": "open source — software whose source code is publicly available",
    "regulation": "regulation — an official rule that controls an activity or industry",
    "release": "release — to make a new product or version available",
    "training": "training — the process in which a model learns from data",
}


def load_config(path: Path) -> dict[str, Any]:
    """Load human-editable settings from YAML."""
    with path.open("r", encoding="utf-8") as config_file:
        return yaml.safe_load(config_file)


def article_for_storage(article: dict[str, Any]) -> dict[str, Any]:
    """Keep only the five permanent article fields in JSON-friendly form."""
    published = article.get("date")
    if isinstance(published, datetime):
        published = published.isoformat()

    return {
        "title": str(article.get("title") or ""),
        "source": str(article.get("source") or ""),
        "date": published,
        "url": str(article.get("url") or ""),
        "summary": str(article.get("summary") or ""),
    }


def article_key(article: dict[str, Any]) -> str:
    """Create a stable duplicate key, preferring the article URL."""
    url = str(article.get("url") or "").strip().rstrip("/")
    if url:
        return f"url:{url.casefold()}"

    # Some RSS entries have no URL. Source + title is a useful fallback.
    source = str(article.get("source") or "").strip().casefold()
    title = str(article.get("title") or "").strip().casefold()
    return f"title:{source}|{title}"


def load_article_store(path: Path) -> list[dict[str, Any]]:
    """Load the permanent article store, or return an empty list initially."""
    if not path.exists():
        return []

    with path.open("r", encoding="utf-8") as article_file:
        data = json.load(article_file)

    if not isinstance(data, list):
        raise ValueError(f"{path.name} must contain a JSON list.")
    return [article for article in data if isinstance(article, dict)]


def merge_articles(
    existing_articles: list[dict[str, Any]],
    collected_articles: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Merge new articles into history and remove duplicate URLs."""
    existing_keys = {article_key(article) for article in existing_articles}
    merged_by_key: dict[str, dict[str, Any]] = {}

    # New records replace old versions of the same URL, which allows an RSS
    # publisher to improve a title or summary after its first publication.
    for article in [*existing_articles, *collected_articles]:
        stored_article = article_for_storage(article)
        merged_by_key[article_key(stored_article)] = stored_article

    new_keys = {article_key(article) for article in collected_articles}
    new_article_count = len(new_keys - existing_keys)
    merged_articles = sorted(
        merged_by_key.values(),
        key=lambda article: str(article.get("date") or ""),
        reverse=True,
    )
    return merged_articles, new_article_count


def write_json_safely(path: Path, articles: list[dict[str, Any]]) -> None:
    """Write JSON through a temporary file to protect the permanent history."""
    temporary_path = path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(articles, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def contains_keyword(text: str, keyword: str) -> bool:
    """Match a keyword without making the search case-sensitive."""
    return keyword.casefold() in text.casefold()


def importance_score(article: dict[str, Any], config: dict[str, Any]) -> int:
    """Give a simple, explainable score to an article."""
    searchable_text = f"{article['title']} {article['summary']}"
    score = 0

    # AI relevance is required; multiple matches increase confidence.
    ai_matches = sum(
        contains_keyword(searchable_text, keyword)
        for keyword in config["ai_keywords"]
    )
    if ai_matches == 0:
        return 0
    score += min(ai_matches, 3)

    # Product launches, research, policy, and business events often matter more.
    important_matches = sum(
        contains_keyword(searchable_text, keyword)
        for keyword in config["important_keywords"]
    )
    score += min(important_matches, 4)

    # A keyword in the title is a stronger signal than one deep in a summary.
    if any(
        contains_keyword(article["title"], keyword)
        for keyword in config["important_keywords"]
    ):
        score += 2

    return score


def select_top_articles(
    articles: list[dict[str, Any]],
    config: dict[str, Any],
    now: datetime | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Keep important articles from the latest completed briefing window."""
    if window_start is None or window_end is None:
        window_start, window_end = current_briefing_window(now)

    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for article in articles:
        article["importance_score"] = importance_score(article, config)
        published_at = article["date"]
        if published_at is not None and published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)

        is_in_briefing_window = (
            published_at is not None
            and window_start <= published_at.astimezone(REPORT_TIMEZONE) < window_end
        )
        is_important = (
            article["importance_score"] >= config["minimum_importance_score"]
        )
        if (
            is_in_briefing_window
            and is_important
            and article["url"] not in seen_urls
        ):
            selected.append(article)
            seen_urls.add(article["url"])

    # Highest score comes first; date breaks ties.
    selected.sort(
        key=lambda item: (
            item["importance_score"],
            item["date"] or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )
    return selected[: config["max_report_items"]]


def shorten(text: str, maximum: int = 260) -> str:
    """Keep RSS descriptions concise without cutting through a word."""
    if not text:
        return "The RSS feed did not provide a summary. Open the article for details."
    if len(text) <= maximum:
        return text
    return text[:maximum].rsplit(" ", 1)[0] + "…"


def why_it_matters(article: dict[str, Any]) -> str:
    """Create a beginner-friendly significance note from the article metadata."""
    return (
        f"This development from {article['source']} may affect how AI is built, "
        "used, or governed. "
        f"{shorten(article['summary'])}"
    )


def technical_explanation(article: dict[str, Any]) -> str:
    """Choose a simple technical explanation based on words in the story."""
    text = f"{article['title']} {article['summary']}".casefold()
    explanations = [
        (
            "multimodal",
            "A multimodal system learns relationships across data types such as "
            "text, images, and audio, so one model can understand or produce more "
            "than one kind of content.",
        ),
        (
            "agent",
            "An AI agent combines a model with tools, memory, and a loop that lets "
            "it choose and perform actions toward a goal.",
        ),
        (
            "open source",
            "Open-source AI makes code or model components available for others to "
            "inspect, run, and adapt, depending on its license.",
        ),
        (
            "model",
            "An AI model is a mathematical system trained on examples. During "
            "inference, it uses learned patterns to generate a prediction or response.",
        ),
    ]
    for keyword, explanation in explanations:
        if keyword in text:
            return explanation
    return (
        "The technical impact depends on the underlying model, training data, "
        "evaluation method, and the way the system is deployed."
    )


def vocabulary_for(article: dict[str, Any], limit: int = 3) -> list[str]:
    """Select useful English terms that occur in the story."""
    text = f"{article['title']} {article['summary']}".casefold()
    found = [definition for word, definition in VOCABULARY.items() if word in text]
    if not found:
        found = [VOCABULARY["model"], VOCABULARY["inference"]]
    return found[:limit]


def learning_question(article: dict[str, Any]) -> str:
    """Create one prompt that encourages active learning."""
    return (
        f"What evidence would you examine to decide whether "
        f"“{article['title']}” represents a major advance rather than a minor update?"
    )


def render_report(
    articles: list[dict[str, Any]],
    report_date: str,
    window_start: datetime,
    window_end: datetime,
) -> str:
    """Turn selected articles into the requested Markdown format."""
    lines = [
        "# AI Morning Brief",
        "",
        f"*Report date: {report_date}*",
        (
            f"*Briefing window: {window_start:%Y-%m-%d %H:%M} to "
            f"{window_end:%Y-%m-%d %H:%M} (Asia/Shanghai)*"
        ),
        "",
        "## Top AI News",
        "",
    ]

    if not articles:
        lines.append(
            "No articles in this briefing window met the importance threshold. "
            "Try lowering `minimum_importance_score` in `config.yaml`."
        )

    for number, article in enumerate(articles, start=1):
        date_text = (
            article["date"].astimezone(REPORT_TIMEZONE).date().isoformat()
            if article["date"]
            else "Unknown"
        )
        vocabulary = "; ".join(vocabulary_for(article))
        lines.extend(
            [
                f"### {number}. [{article['title']}]({article['url']})",
                "",
                f"- **Company/source:** {article['source']}",
                f"- **Published:** {date_text}",
                f"- **Why it matters:** {why_it_matters(article)}",
                f"- **Technical explanation:** {technical_explanation(article)}",
                f"- **English vocabulary:** {vocabulary}",
                f"- **My learning question:** {learning_question(article)}",
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    """Run the complete Version 0.1 pipeline."""
    parser = argparse.ArgumentParser(description="Create an AI morning briefing.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to the YAML configuration file.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    print("Collecting RSS articles...")
    articles = collect_news(config["feeds"], config["max_articles_per_feed"])
    if not articles:
        raise RuntimeError(
            "No RSS articles were collected. Existing reports and stored data "
            "were left unchanged."
        )

    briefing_start, briefing_end = current_briefing_window()
    top_articles = select_top_articles(
        articles,
        config,
        window_start=briefing_start,
        window_end=briefing_end,
    )

    report_date = briefing_end.date().isoformat()
    output_directory = PROJECT_DIR / "output"
    output_directory.mkdir(exist_ok=True)
    output_path = output_directory / f"{report_date}_AI_Brief.md"
    data_path = output_directory / f"{report_date}_articles.json"
    permanent_data_path = output_directory / "articles.json"

    # Preserve today's snapshot and merge it into the permanent, deduplicated store.
    saved_articles = [article_for_storage(article) for article in articles]
    existing_articles = load_article_store(permanent_data_path)
    permanent_articles, new_article_count = merge_articles(
        existing_articles,
        saved_articles,
    )
    write_json_safely(data_path, saved_articles)
    write_json_safely(permanent_data_path, permanent_articles)
    new_db_articles, new_db_versions = sync_articles_from_json(
        permanent_data_path,
        DEFAULT_DB_PATH,
    )
    output_path.write_text(
        render_report(
            top_articles,
            report_date,
            briefing_start,
            briefing_end,
        ),
        encoding="utf-8",
    )

    print(f"Collected {len(articles)} articles.")
    print(f"Added {new_article_count} new articles.")
    print(f"Permanent article total: {len(permanent_articles)}.")
    print(f"Selected {len(top_articles)} important articles.")
    print(f"Daily snapshot saved to: {data_path}")
    print(f"Dashboard data saved to: {permanent_data_path}")
    print(
        f"SQLite updated: {new_db_articles} new articles, "
        f"{new_db_versions} new versions."
    )
    print(f"Report saved to: {output_path}")


if __name__ == "__main__":
    main()
