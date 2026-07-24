"""
Entry point: scrape tweets then run the three-layer sentiment pipeline.

Usage
-----
    python main.py <username> [--max-tweets N] [--no-dl] [--csv PATH]
                              [--force-rescrape] [--history]

Examples
--------
    python main.py elonmusk
    python main.py NASA --max-tweets 50
    python main.py openai --no-dl              # skip DistilBERT (faster)
    python main.py openai --csv results.csv    # save full DataFrame to CSV
    python main.py openai --force-rescrape     # bypass cache, always hit X live
    python main.py openai --history            # print stored tweets without scraping
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from playwright.sync_api import Error as PlaywrightError

from scraper import scrape_profile_tweets
from scraper_health import ScraperBrokenError, build_health_report, check_health
from sentiment_analysis import analyze_tweets, summarize
from storage import (
    get_all_tweets,
    get_cached_tweets,
    get_last_successful_scrape_time,
    init_db,
    log_scrape_job,
    upsert_tweets,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape tweets and run sentiment analysis."
    )
    parser.add_argument("username", help="Twitter/X username (with or without @)")
    parser.add_argument(
        "--max-tweets",
        type=int,
        default=25,
        metavar="N",
        help="Maximum number of tweets to collect (default: 25)",
    )
    parser.add_argument(
        "--no-dl",
        action="store_true",
        help="Skip the DistilBERT deep-learning layer (faster, uses only TextBlob + VADER)",
    )
    parser.add_argument(
        "--csv",
        metavar="PATH",
        default=None,
        help="If provided, save the full results DataFrame to this CSV file",
    )
    parser.add_argument(
        "--force-rescrape",
        action="store_true",
        help="Bypass the cache and always perform a live scrape from X",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Print all stored tweets for this username from the DB without scraping",
    )
    return parser.parse_args()


def _hours_ago(dt: datetime) -> float:
    """Return how many hours ago *dt* occurred relative to now (UTC)."""
    delta = datetime.now(timezone.utc) - dt
    return delta.total_seconds() / 3600


def _print_results(df, args) -> None:
    """Print the per-tweet table and JSON summary."""
    display_cols = [
        "username", "final_label", "final_confidence",
        "vader_compound", "textblob_polarity", "text",
    ]
    available = [c for c in display_cols if c in df.columns]
    print(df[available].to_string(index=False, max_colwidth=60))

    print("\n── Summary ────────────────────────────────────────────")
    summary = summarize(df)
    print(json.dumps(summary, indent=2))

    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"\nFull results saved to: {args.csv}")


def main() -> None:
    init_db()

    args = parse_args()
    clean_username = args.username.strip().lstrip("@")

    if args.history:
        print(f"[DB] Fetching stored tweets for @{clean_username} …")
        df = get_all_tweets(clean_username)
        if df.empty:
            print(f"No stored tweets found for @{clean_username}.")
            sys.exit(0)
        print(f"      Found {len(df)} stored tweet(s).\n")
        _print_results(df, args)
        return

    if not args.force_rescrape:
        cached = get_cached_tweets(clean_username)
        if cached is not None:
            last_scrape = get_last_successful_scrape_time(clean_username)
            age_str = f"{_hours_ago(last_scrape):.1f}h" if last_scrape else "?"
            print(
                f"[CACHE HIT] Returning cached results for @{clean_username} "
                f"(scraped {age_str} ago)."
            )
            _print_results(cached, args)
            return

    print(f"[1/3] Scraping tweets for @{clean_username} …")

    try:
        result = scrape_profile_tweets(clean_username, max_tweets=args.max_tweets)
    except (PlaywrightError, ValueError) as exc:
        print(f"\n[ERROR] Scrape failed: {exc}")
        log_scrape_job(clean_username, tweet_count=0, status="error", error_message=str(exc))
        sys.exit(1)

    tweets = result["tweets"]

    health = build_health_report(
        username=clean_username,
        tweets_found=len(tweets),
        articles_seen=result["articles_seen"],
        skip_log=result["skip_reasons"],
        zero_text_count=result["zero_text_count"],
    )

    try:
        check_health(
            health,
            max_tweets=args.max_tweets,
            max_scrolls=10,
            scroll_count=result["scroll_count"],
        )
    except ScraperBrokenError as exc:
        print(f"\n[SCRAPER BROKEN] {exc}")
        print(
            "\nRemediation steps:\n"
            "  1. Open x.com manually and inspect the tweet element.\n"
            "  2. Verify the data-testid='tweetText' selector still exists.\n"
            "  3. Update extract_tweet_text() in scraper.py if the selector changed.\n"
            "  4. Run: playwright install chromium  (to update the browser binary)\n"
            "  5. Re-run with --force-rescrape after fixing the selector."
        )
        log_scrape_job(clean_username, tweet_count=0, status="error", error_message=str(exc))
        sys.exit(1)

    for warning_msg in health["warnings"]:
        print(f"[WARN] {warning_msg}")

    if not tweets:
        print("No tweets found. The profile may be private or the username is wrong.")
        log_scrape_job(clean_username, tweet_count=0, status="error", error_message="No tweets found")
        sys.exit(1)

    print(f"      Collected {len(tweets)} tweet(s).")

    dl_msg = "TextBlob + VADER only (--no-dl)" if args.no_dl else "TextBlob + VADER + DistilBERT"
    print(f"[2/3] Running sentiment analysis ({dl_msg}) …")
    df = analyze_tweets(tweets, use_dl=not args.no_dl)

    new_rows = upsert_tweets(df)
    log_scrape_job(clean_username, tweet_count=len(tweets), status="success")
    print(f"      Stored {new_rows} new tweet(s) to DB.")

    print("[3/3] Results\n")
    _print_results(df, args)


if __name__ == "__main__":
    main()
