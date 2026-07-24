"""
Scraper health reporting for detecting X/Twitter DOM drift and structural failures.

Use build_health_report() to construct a report from scrape metadata, then
pass it to check_health() to emit warnings or raise ScraperBrokenError when
the scraper is likely broken or degraded.
"""

from __future__ import annotations

import warnings
from datetime import datetime, timezone
from typing import TypedDict


class ScrapeHealthReport(TypedDict):
    """Structured metadata about a single scrape run for health evaluation."""

    username: str
    timestamp: str
    tweets_found: int
    articles_seen: int
    skip_reasons: dict[str, int]
    zero_text_count: int
    warnings: list[str]
    likely_broken: bool


class ScraperBrokenError(Exception):
    """
    Raised when the scraper detects a structural failure that strongly suggests
    X/Twitter has changed its DOM layout.

    Catching this error should trigger a human-readable message with remediation
    steps rather than a silent failure or empty result set.
    """


def build_health_report(
    username: str,
    tweets_found: int,
    articles_seen: int,
    skip_log: dict[str, int],
    zero_text_count: int,
) -> ScrapeHealthReport:
    """
    Construct a ScrapeHealthReport from raw scrape metadata.

    Parameters
    ----------
    username:
        Twitter/X handle that was scraped.
    tweets_found:
        Number of TweetRecords successfully extracted.
    articles_seen:
        Total number of article elements encountered across all scrolls.
    skip_log:
        Counts of articles skipped, keyed by reason (``'pinned'``, ``'reply'``,
        ``'promoted'``).
    zero_text_count:
        Articles that were not skipped but yielded no extractable tweet text.
    """
    return {
        "username": username.strip().lstrip("@"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tweets_found": tweets_found,
        "articles_seen": articles_seen,
        "skip_reasons": dict(skip_log),
        "zero_text_count": zero_text_count,
        "warnings": [],
        "likely_broken": False,
    }


def check_health(
    report: ScrapeHealthReport,
    max_tweets: int = 25,
    max_scrolls: int = 10,
    scroll_count: int = 0,
) -> None:
    """
    Evaluate a ScrapeHealthReport and surface warnings or a hard error.

    Parameters
    ----------
    report:
        Report produced by build_health_report(); mutated in-place to append
        warning strings and set ``likely_broken``.
    max_tweets:
        The requested tweet ceiling (used for the under-delivery check).
    max_scrolls:
        Maximum scrolls allowed (used for the under-delivery check).
    scroll_count:
        Actual number of scrolls performed during the scrape.

    Raises
    ------
    ScraperBrokenError
        When articles were found but zero tweets were extracted — this almost
        always means X changed its ``data-testid='tweetText'`` selector.
    """
    articles_seen = report["articles_seen"]
    tweets_found = report["tweets_found"]
    zero_text_count = report["zero_text_count"]

    if articles_seen > 0 and tweets_found == 0:
        report["likely_broken"] = True
        raise ScraperBrokenError(
            "Scraper found articles but extracted 0 tweets — "
            "X may have changed its DOM. Check data-testid='tweetText' selector."
        )

    if articles_seen > 0 and zero_text_count / articles_seen > 0.5:
        msg = (
            "More than 50% of articles had no extractable text — "
            "selector drift likely."
        )
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        report["warnings"].append(msg)

    if articles_seen == 0:
        msg = (
            "No article elements found — X may have changed its page structure "
            "or a login wall appeared."
        )
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        report["warnings"].append(msg)

    if tweets_found < max_tweets * 0.5 and scroll_count >= max_scrolls:
        msg = (
            "Retrieved far fewer tweets than requested — possible rate limit, "
            "private profile, or scroll detection."
        )
        warnings.warn(msg, RuntimeWarning, stacklevel=2)
        report["warnings"].append(msg)
