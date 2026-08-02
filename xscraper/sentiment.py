from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

_analyzer = SentimentIntensityAnalyzer()


def analyze(text: str) -> tuple[str, float, str]:
    compound = float(_analyzer.polarity_scores(text)["compound"])
    if compound >= 0.05:
        label = "positive"
    elif compound <= -0.05:
        label = "negative"
    else:
        label = "neutral"
    try:
        analyzer_version = version("vaderSentiment")
    except PackageNotFoundError:
        analyzer_version = "unknown"
    return label, compound, analyzer_version
