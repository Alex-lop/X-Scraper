"""
Minimal Flask API that exposes the scraper and storage layer to the frontend.

Start with:
    python server.py

Endpoints
---------
POST /api/scrape
    Run cache → scrape → analyze → store pipeline for a username or URL.
GET  /api/history?username=<handle>
    Return all cached tweets for a handle from the DB.
GET  /api/health
    Server status and DB tweet count.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from flask import Flask, jsonify, request
from flask_cors import CORS
from playwright.sync_api import Error as PlaywrightError

from scraper import scrape_profile_tweets
from scraper_health import ScraperBrokenError, build_health_report, check_health
from sentiment_analysis import analyze_tweets
from storage import (
    get_all_tweets,
    get_cached_tweets,
    get_last_successful_scrape_time,
    init_db,
    log_scrape_job,
    upsert_tweets,
)

app = Flask(__name__)
CORS(app)

init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_username(source_value: str) -> str:
    """
    Derive a bare Twitter/X username from a raw sourceValue string.

    Handles:
    - Plain handles:  "elonmusk", "@elonmusk"
    - Profile URLs:   "https://x.com/elonmusk", "https://twitter.com/elonmusk"
    - Status URLs:    "https://x.com/elonmusk/status/12345" → "elonmusk"
    """
    sv = source_value.strip()

    url_match = re.search(
        r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)", sv
    )
    if url_match:
        return url_match.group(1)

    return sv.lstrip("@")


def _df_to_tweet_list(df, cache_hit: bool = False) -> list[dict]:
    """Convert an analyzed DataFrame to the JSON shape expected by app.js."""
    records = []
    for _, row in df.iterrows():
        records.append(
            {
                "username": row.get("username", ""),
                "text": row.get("text", ""),
                "createdAt": row.get("created_at") or "",
                "likeCount": 0,
                "replyCount": 0,
                "retweetCount": 0,
                "url": row.get("url") or "",
                "finalLabel": row.get("final_label", "neutral"),
                "finalConfidence": float(row.get("final_confidence", 0.0)),
            }
        )
    return records


def _hours_ago(dt: datetime) -> float:
    delta = datetime.now(timezone.utc) - dt
    return delta.total_seconds() / 3600


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/api/scrape", methods=["POST"])
def scrape():
    """
    Run the full cache → scrape → analyze → store pipeline.

    Request JSON
    ------------
    {
        "sourceValue":  str,         # username, @handle, or x.com URL
        "maxTweets":    int,         # default 25
        "includeType":  str,         # "all" | "original" | "replies" | "media"
        "startDate":    str | null,
        "endDate":      str | null
    }

    Response JSON
    -------------
    { "tweets": [...], "cacheHit": bool }   on success
    { "error": "..." }                       on failure (4xx / 5xx)
    """
    body = request.get_json(silent=True) or {}

    source_value: str = (body.get("sourceValue") or "").strip()
    if not source_value:
        return jsonify({"error": "sourceValue is required."}), 400

    max_tweets: int = int(body.get("maxTweets") or 25)
    username = _extract_username(source_value)

    if not username:
        return jsonify({"error": "Could not determine a username from the provided source."}), 400

    cached = get_cached_tweets(username)
    if cached is not None:
        last_scrape = get_last_successful_scrape_time(username)
        age_str = f"{_hours_ago(last_scrape):.1f}h" if last_scrape else "?"
        tweets_list = _df_to_tweet_list(cached, cache_hit=True)
        return jsonify({"tweets": tweets_list, "cacheHit": True, "cacheAge": age_str})

    try:
        result = scrape_profile_tweets(username, max_tweets=max_tweets)
    except ValueError as exc:
        log_scrape_job(username, tweet_count=0, status="error", error_message=str(exc))
        return jsonify({"error": str(exc)}), 503
    except PlaywrightError as exc:
        msg = f"Browser error while scraping @{username}: {exc}"
        log_scrape_job(username, tweet_count=0, status="error", error_message=msg)
        return jsonify({"error": msg}), 503
    except Exception as exc:
        msg = f"Unexpected error: {exc}"
        log_scrape_job(username, tweet_count=0, status="error", error_message=msg)
        return jsonify({"error": msg}), 500

    tweets = result["tweets"]

    health = build_health_report(
        username=username,
        tweets_found=len(tweets),
        articles_seen=result["articles_seen"],
        skip_log=result["skip_reasons"],
        zero_text_count=result["zero_text_count"],
    )

    try:
        check_health(
            health,
            max_tweets=max_tweets,
            max_scrolls=10,
            scroll_count=result["scroll_count"],
        )
    except ScraperBrokenError as exc:
        log_scrape_job(username, tweet_count=0, status="error", error_message=str(exc))
        return jsonify(
            {
                "error": (
                    f"{exc} "
                    "Try updating Playwright (playwright install chromium) and "
                    "verify the data-testid='tweetText' selector in scraper.py."
                )
            }
        ), 503

    if not tweets:
        log_scrape_job(username, tweet_count=0, status="error", error_message="No tweets found")
        return jsonify(
            {"error": f"No tweets found for @{username}. The profile may be private or does not exist."}
        ), 404

    df = analyze_tweets(tweets, use_dl=False)
    upsert_tweets(df)
    log_scrape_job(username, tweet_count=len(tweets), status="success")

    tweets_list = _df_to_tweet_list(df, cache_hit=False)
    return jsonify({"tweets": tweets_list, "cacheHit": False})


@app.route("/api/history")
def history():
    """
    Return all stored tweets for a username.

    Query params
    ------------
    username : str  (required)
    """
    username = (request.args.get("username") or "").strip().lstrip("@")
    if not username:
        return jsonify({"error": "username query parameter is required."}), 400

    df = get_all_tweets(username)
    if df.empty:
        return jsonify({"tweets": [], "message": f"No stored tweets for @{username}."})

    return jsonify({"tweets": _df_to_tweet_list(df)})


@app.route("/api/health")
def health():
    """Return server status and DB tweet count."""
    df = get_all_tweets()
    return jsonify(
        {
            "status": "ok",
            "tweet_count": len(df),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
