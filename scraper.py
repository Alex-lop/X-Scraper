# Scraper for Twitter/X public pages.
#
# This file should stay focused on gathering raw tweet data.
# Sentiment analysis and storage should live in separate modules later.

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import TypedDict
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError, Locator, Page, sync_playwright


class TweetRecord(TypedDict):
    #Basic shape of the raw data we want to collect.

    username: str
    text: str
    url: str | None
    created_at: str | None
    scraped_at: str


class ScrapeResult(TypedDict):
    """
    Full output of a scrape run, combining raw tweet records with metadata
    needed by scraper_health to evaluate whether the scrape was healthy.
    """

    tweets: list[TweetRecord]
    articles_seen: int
    skip_reasons: dict[str, int]
    zero_text_count: int
    scroll_count: int


def build_profile_url(username: str) -> str:
    # Create the public profile URL for a Twitter/X username.
    clean_username = username.strip().lstrip("@")
    return f"https://x.com/{clean_username}"


def safe_inner_text(locator: Locator) -> str:
    """Read text from a locator without crashing if X changes or detaches the node."""
    try:
        return locator.inner_text().strip()
    except PlaywrightError:
        return ""


def safe_get_attribute(locator: Locator, attribute_name: str) -> str | None:
    """Read an attribute from a locator without crashing on missing/detached nodes."""
    try:
        return locator.get_attribute(attribute_name)
    except PlaywrightError:
        return None


def article_text_lines(article: Locator) -> list[str]:
    """Break the article's visible text into clean lines for label checks."""
    text = safe_inner_text(article)
    return [line.strip() for line in text.splitlines() if line.strip()]


def extract_tweet_url(article: Locator) -> str | None:
    #Find the canonical tweet URL from links inside an article.
    links = article.locator('a[href*="/status/"]').all()

    for link in links:
        href = safe_get_attribute(link, "href")

        if href:
            return urljoin("https://x.com/", href)

    return None


def extract_tweet_created_at(article: Locator) -> str | None:
    # Use the first time element because a tweet article may contain extra nested timestamps.
    timestamp = article.locator("time").first

    if timestamp.count() == 0:
        return None

    return safe_get_attribute(timestamp, "datetime") # X exposes tweet timestamps through the datetime attribute.


def extract_tweet_text(article: Locator) -> str | None:
    #Extract only the tweet body text instead of the full article labels.
    tweet_text = article.locator('[data-testid="tweetText"]').first

    if tweet_text.count() == 0:
        return None

    text = safe_inner_text(tweet_text) # strip() removes whitespace and inner_text() returns visible text.

    if not text:
        return None

    return text


def is_pinned_article(article: Locator) -> bool:
    """Detect profile-pinned posts from X's visible labels."""
    lines = article_text_lines(article)
    return any(line.lower() in {"pinned", "pinned post"} for line in lines[:4])


def is_reply_article(article: Locator) -> bool:
    """Detect reply tweets from X's visible reply context."""
    lines = article_text_lines(article)
    return any(line.startswith("Replying to @") or line == "Replying to" for line in lines)


def is_promoted_article(article: Locator) -> bool:
    """Detect promoted/ad articles from X's visible labels."""
    lines = article_text_lines(article)
    return any(line.lower() in {"promoted", "ad"} for line in lines)


def should_skip_article(
    article: Locator,
    skip_pinned: bool = True,
    skip_replies: bool = True,
    skip_promoted: bool = True,
) -> bool:
    """
    Decide whether an article should be ignored before extraction.

    TODO:
    - Replace broad text-label checks with stable selectors if X exposes them.
    - Make these options configurable from main.py once the CLI/pipeline exists.
    - Save skip reasons during debugging if we need to inspect missed tweets.
    """
    if skip_pinned and is_pinned_article(article):
        return True

    if skip_replies and is_reply_article(article):
        return True

    if skip_promoted and is_promoted_article(article):
        return True

    return False


def _get_skip_reason(
    article: Locator,
    skip_pinned: bool = True,
    skip_replies: bool = True,
    skip_promoted: bool = True,
) -> str | None:
    """Return the skip reason string for an article, or None if it should be processed."""
    if skip_pinned and is_pinned_article(article):
        return "pinned"
    if skip_replies and is_reply_article(article):
        return "reply"
    if skip_promoted and is_promoted_article(article):
        return "promoted"
    return None


def tweet_dedupe_key(tweet: TweetRecord) -> str:
    """Prefer a tweet URL for dedupe, then fall back to timestamp/text."""
    if tweet["url"]:
        return tweet["url"]

    if tweet["created_at"]:
        return f'{tweet["username"]}:{tweet["created_at"]}:{tweet["text"]}'

    return f'{tweet["username"]}:{tweet["text"]}'


def extract_tweet_from_article(article: Locator, username: str) -> TweetRecord | None:
    """
    Convert one visible tweet article into a raw TweetRecord.
    """
    if should_skip_article(article):
        return None

    clean_username = username.strip().lstrip("@")
    text = extract_tweet_text(article)

    if text is None:
        return None

    url = extract_tweet_url(article)
    created_at = extract_tweet_created_at(article)
    scraped_at = datetime.now(timezone.utc).isoformat()

    return {
        "username": clean_username,
        "text": text,
        "url": url,
        "created_at": created_at,
        "scraped_at": scraped_at,
    }


def should_continue_scrolling(
    tweets_found: int,
    max_tweets: int,
    scroll_count: int,
    max_scrolls: int,
) -> bool:
    """
    Decide whether the scraper should keep scrolling.

    TODO:
    - Add a "no new tweets after N scrolls" stop condition.
    - Add detection for login walls, rate-limit pages, or empty profiles.
    - Add optional time-based limits for long-running scrape jobs.
    """
    if tweets_found >= max_tweets:
        return False

    if scroll_count >= max_scrolls:
        return False

    return True


def scroll_timeline(page: Page, scroll_pixels: int, pause_seconds: float) -> None:
    """
    Scroll the page once and wait for new timeline content to load.

    TODO:
    - Replace sleep with a Playwright wait when we know the best page signal.
    - Randomize scroll/pause slightly if scraping too mechanically causes issues.
    - Detect whether the page height changed after scrolling.
    """
    page.mouse.wheel(0, scroll_pixels)
    time.sleep(pause_seconds)


def scrape_profile_tweets(
    username: str,
    max_tweets: int = 25,
    max_scrolls: int = 10,
    scroll_pixels: int = 2_500,
    pause_seconds: float = 2,
) -> ScrapeResult:
    """
    Scrape raw tweet-like article blocks from a public profile.

    Basic flow:
    1. Open a headless Chromium browser.
    2. Visit the public profile page.
    3. Detect login walls before proceeding.
    4. Collect visible tweet articles, tracking health metadata.
    5. Scroll slowly to load more.
    6. Return a ScrapeResult with raw records and health metadata.

    Raises
    ------
    ValueError
        If X presents a login wall instead of the public profile.
    PlaywrightError
        On network timeout or other browser-level failures.
    """
    profile_url = build_profile_url(username)
    clean_username = username.strip().lstrip("@")
    tweets: list[TweetRecord] = []
    seen_tweets: set[str] = set()
    articles_seen: int = 0
    skip_log: dict[str, int] = {}
    zero_text_count: int = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page: Page = browser.new_page()

        page.goto(profile_url, wait_until="networkidle", timeout=60_000)
        time.sleep(2)

        final_url = page.url
        page_title = page.title()
        if "/i/flow/login" in final_url or "Log in" in page_title:
            browser.close()
            raise ValueError(
                f"X is showing a login wall for @{clean_username}. "
                "The scraper cannot access this profile without authentication. "
                "Try again later or check if the profile is private."
            )

        scroll_count = 0

        while should_continue_scrolling(
            tweets_found=len(tweets),
            max_tweets=max_tweets,
            scroll_count=scroll_count,
            max_scrolls=max_scrolls,
        ):
            articles = page.locator("article").all()

            for article in articles:
                articles_seen += 1

                skip_reason = _get_skip_reason(article)
                if skip_reason:
                    skip_log[skip_reason] = skip_log.get(skip_reason, 0) + 1
                    continue

                text = extract_tweet_text(article)
                if text is None:
                    zero_text_count += 1
                    continue

                url = extract_tweet_url(article)
                created_at = extract_tweet_created_at(article)
                scraped_at = datetime.now(timezone.utc).isoformat()

                tweet: TweetRecord = {
                    "username": clean_username,
                    "text": text,
                    "url": url,
                    "created_at": created_at,
                    "scraped_at": scraped_at,
                }

                dedupe_key = tweet_dedupe_key(tweet)
                if dedupe_key in seen_tweets:
                    continue

                seen_tweets.add(dedupe_key)
                tweets.append(tweet)

                if len(tweets) >= max_tweets:
                    break

            scroll_timeline(page, scroll_pixels, pause_seconds)
            scroll_count += 1

        browser.close()

    return {
        "tweets": tweets,
        "articles_seen": articles_seen,
        "skip_reasons": skip_log,
        "zero_text_count": zero_text_count,
        "scroll_count": scroll_count,
    }
