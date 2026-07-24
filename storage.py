"""
SQLite-backed persistence and caching layer for tweet records.

The database file (tweets.db) is created automatically on first run.
All public functions are safe to call repeatedly; they are idempotent
where the underlying operation allows it.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd

DB_PATH = os.path.join(os.path.dirname(__file__), "tweets.db")

_CREATE_TWEETS_TABLE = """
CREATE TABLE IF NOT EXISTS tweets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT    NOT NULL,
    text             TEXT    NOT NULL,
    url              TEXT    UNIQUE,
    created_at       TEXT,
    scraped_at       TEXT    NOT NULL,
    textblob_polarity    REAL,
    textblob_subjectivity REAL,
    textblob_label       TEXT,
    vader_compound       REAL,
    vader_label          TEXT,
    dl_label             TEXT,
    dl_confidence        REAL,
    dl_backend           TEXT,
    final_label          TEXT,
    final_confidence     REAL
)
"""

_CREATE_SCRAPE_JOBS_TABLE = """
CREATE TABLE IF NOT EXISTS scrape_jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL,
    scraped_at    TEXT    NOT NULL,
    tweet_count   INTEGER NOT NULL,
    status        TEXT    NOT NULL,
    error_message TEXT
)
"""

_TWEET_COLUMNS = [
    "username", "text", "url", "created_at", "scraped_at",
    "textblob_polarity", "textblob_subjectivity", "textblob_label",
    "vader_compound", "vader_label",
    "dl_label", "dl_confidence", "dl_backend",
    "final_label", "final_confidence",
]


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TWEETS_TABLE)
    conn.execute(_CREATE_SCRAPE_JOBS_TABLE)
    conn.commit()


def init_db() -> None:
    """
    Create database tables if they don't exist.

    Safe to call on every startup. If the database file is corrupted,
    it is deleted and recreated from scratch.
    """
    try:
        conn = _connect()
        try:
            _create_tables(conn)
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        conn = _connect()
        try:
            _create_tables(conn)
        finally:
            conn.close()


def upsert_tweets(df: pd.DataFrame) -> int:
    """
    Insert new tweets from an analyzed DataFrame, skipping duplicates by URL.

    Parameters
    ----------
    df:
        DataFrame produced by sentiment_analysis.analyze_tweets().

    Returns
    -------
    int
        Number of newly inserted rows (duplicates silently skipped).
    """
    if df.empty:
        return 0

    insert_cols = [c for c in _TWEET_COLUMNS if c in df.columns]
    col_names = ", ".join(insert_cols)
    placeholders = ", ".join("?" for _ in insert_cols)
    sql = f"INSERT OR IGNORE INTO tweets ({col_names}) VALUES ({placeholders})"

    records = (
        df[insert_cols]
        .where(df[insert_cols].notna(), other=None)
        .values.tolist()
    )

    conn = _connect()
    try:
        before = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        conn.executemany(sql, records)
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM tweets").fetchone()[0]
        return after - before
    finally:
        conn.close()


def get_cached_tweets(username: str, max_age_hours: int = 6) -> pd.DataFrame | None:
    """
    Return cached tweets for a username if a recent successful scrape exists.

    Parameters
    ----------
    username:
        Twitter/X handle (with or without leading @).
    max_age_hours:
        How old (in hours) the most recent successful scrape job may be
        before the cache is considered stale.

    Returns
    -------
    pd.DataFrame | None
        A DataFrame of cached tweets, or None if no fresh cache exists.
        None signals the caller that a live scrape is required.
    """
    clean = username.strip().lstrip("@")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).isoformat()

    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT id FROM scrape_jobs
            WHERE username = ? AND status = 'success' AND scraped_at >= ?
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            (clean, cutoff),
        ).fetchone()

        if row is None:
            return None

        df = pd.read_sql_query(
            "SELECT * FROM tweets WHERE username = ? ORDER BY created_at DESC",
            conn,
            params=(clean,),
        )
        return df if not df.empty else None
    finally:
        conn.close()


def get_last_successful_scrape_time(username: str) -> datetime | None:
    """
    Return the UTC datetime of the most recent successful scrape for a username,
    or None if no successful scrape has ever been recorded.
    """
    clean = username.strip().lstrip("@")
    conn = _connect()
    try:
        row = conn.execute(
            """
            SELECT scraped_at FROM scrape_jobs
            WHERE username = ? AND status = 'success'
            ORDER BY scraped_at DESC
            LIMIT 1
            """,
            (clean,),
        ).fetchone()

        if row is None:
            return None

        return datetime.fromisoformat(row[0])
    finally:
        conn.close()


def log_scrape_job(
    username: str,
    tweet_count: int,
    status: str,
    error_message: str | None = None,
) -> None:
    """
    Record a scrape job result in the scrape_jobs table.

    Parameters
    ----------
    username:
        Twitter/X handle.
    tweet_count:
        Number of tweets collected (0 on error).
    status:
        ``'success'`` or ``'error'``.
    error_message:
        Exception message on failure; None on success.
    """
    clean = username.strip().lstrip("@")
    scraped_at = datetime.now(timezone.utc).isoformat()

    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO scrape_jobs (username, scraped_at, tweet_count, status, error_message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (clean, scraped_at, tweet_count, status, error_message),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_tweets(username: str | None = None) -> pd.DataFrame:
    """
    Return all stored tweets, optionally filtered by username.

    Parameters
    ----------
    username:
        If provided, only tweets for this handle are returned.
        Pass None (default) to retrieve the full table.
    """
    conn = _connect()
    try:
        if username is not None:
            clean = username.strip().lstrip("@")
            return pd.read_sql_query(
                "SELECT * FROM tweets WHERE username = ? ORDER BY created_at DESC",
                conn,
                params=(clean,),
            )
        return pd.read_sql_query(
            "SELECT * FROM tweets ORDER BY scraped_at DESC",
            conn,
        )
    finally:
        conn.close()
