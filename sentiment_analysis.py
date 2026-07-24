# Sentiment analysis pipeline for tweet records.
#
# Three-layer approach:
#   Layer 1 – TextBlob  (pandas, rule-based, polarity + subjectivity baseline)
#   Layer 2 – VADER     (pandas, rule-based, tuned for short social-media text)
#   Layer 3 – DistilBERT via HuggingFace transformers
#              (deep-learning; auto-selects PyTorch or TensorFlow at runtime)
#
# The final label is a majority vote across all three layers.
# All intermediate scores are kept in the returned DataFrame for inspection.

from __future__ import annotations

import warnings
from typing import Literal, TypedDict

import pandas as pd
from textblob import TextBlob
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from scraper import TweetRecord

SentimentLabel = Literal["positive", "negative", "neutral"]
DLBackend = Literal["pytorch", "tensorflow", "none"]

# ── Type describing one fully-analyzed tweet ──────────────────────────────────

class SentimentRecord(TypedDict):
    # Raw tweet fields (copied from TweetRecord)
    username: str
    text: str
    url: str | None
    created_at: str | None
    scraped_at: str

    # TextBlob scores
    textblob_polarity: float        # -1.0 (very negative) → +1.0 (very positive)
    textblob_subjectivity: float    #  0.0 (objective)     →  1.0 (subjective)
    textblob_label: SentimentLabel

    # VADER scores
    vader_positive: float
    vader_neutral: float
    vader_negative: float
    vader_compound: float           # -1.0 → +1.0 (overall VADER score)
    vader_label: SentimentLabel

    # Deep-learning scores (DistilBERT)
    dl_label: SentimentLabel
    dl_confidence: float            # 0.0 → 1.0
    dl_backend: DLBackend           # which framework was used

    # Aggregated result
    final_label: SentimentLabel
    final_confidence: float         # fraction of the three layers that agreed


# ── Backend detection ─────────────────────────────────────────────────────────

def detect_dl_backend() -> DLBackend:
    """
    Return 'pytorch' or 'tensorflow' depending on which is importable,
    preferring PyTorch.  Returns 'none' if neither is available.
    """
    try:
        import torch  # noqa: F401
        return "pytorch"
    except ImportError:
        pass

    try:
        import tensorflow  # noqa: F401
        return "tensorflow"
    except ImportError:
        pass

    return "none"


# ── Layer 1 – TextBlob ────────────────────────────────────────────────────────

_TEXTBLOB_NEUTRAL_THRESHOLD = 0.05  # polarity within ±0.05 is called neutral


def _textblob_label(polarity: float) -> SentimentLabel:
    if polarity > _TEXTBLOB_NEUTRAL_THRESHOLD:
        return "positive"
    if polarity < -_TEXTBLOB_NEUTRAL_THRESHOLD:
        return "negative"
    return "neutral"


def analyze_textblob(df: pd.DataFrame, text_col: str = "text") -> pd.DataFrame:
    """
    Add TextBlob polarity, subjectivity, and label columns to *df* in-place.
    Returns the same DataFrame for chaining.
    """
    blobs = df[text_col].apply(TextBlob)
    df["textblob_polarity"] = blobs.apply(lambda b: b.sentiment.polarity)
    df["textblob_subjectivity"] = blobs.apply(lambda b: b.sentiment.subjectivity)
    df["textblob_label"] = df["textblob_polarity"].apply(_textblob_label)
    return df


# ── Layer 2 – VADER ───────────────────────────────────────────────────────────

_VADER_POSITIVE_THRESHOLD = 0.05
_VADER_NEGATIVE_THRESHOLD = -0.05

_vader_analyzer = SentimentIntensityAnalyzer()


def _vader_label(compound: float) -> SentimentLabel:
    if compound >= _VADER_POSITIVE_THRESHOLD:
        return "positive"
    if compound <= _VADER_NEGATIVE_THRESHOLD:
        return "negative"
    return "neutral"


def analyze_vader(df: pd.DataFrame, text_col: str = "text") -> pd.DataFrame:
    """
    Add VADER pos/neu/neg/compound scores and label columns to *df* in-place.
    Returns the same DataFrame for chaining.
    """
    scores = df[text_col].apply(_vader_analyzer.polarity_scores)
    df["vader_positive"] = scores.apply(lambda s: s["pos"])
    df["vader_neutral"] = scores.apply(lambda s: s["neu"])
    df["vader_negative"] = scores.apply(lambda s: s["neg"])
    df["vader_compound"] = scores.apply(lambda s: s["compound"])
    df["vader_label"] = df["vader_compound"].apply(_vader_label)
    return df


# ── Layer 3 – DistilBERT (HuggingFace transformers) ──────────────────────────

_DL_MODEL = "distilbert-base-uncased-finetuned-sst-2-english"
_MAX_DL_TOKEN_LENGTH = 512  # DistilBERT hard limit; tweets are well under this


def _load_dl_pipeline(backend: DLBackend):
    """
    Load the HuggingFace sentiment pipeline for the detected backend.
    Lazy-loads so startup isn't slow when DL analysis is skipped.
    """
    from transformers import pipeline as hf_pipeline  # imported lazily

    framework = "pt" if backend == "pytorch" else "tf"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # suppress transformers version noise
        return hf_pipeline(
            "sentiment-analysis",
            model=_DL_MODEL,
            framework=framework,
            truncation=True,
            max_length=_MAX_DL_TOKEN_LENGTH,
        )


def _dl_label(raw_label: str) -> SentimentLabel:
    """Map HuggingFace POSITIVE/NEGATIVE labels to our SentimentLabel type."""
    return "positive" if raw_label.upper() == "POSITIVE" else "negative"


def analyze_dl(
    df: pd.DataFrame,
    text_col: str = "text",
    batch_size: int = 32,
) -> pd.DataFrame:
    """
    Add deep-learning label and confidence columns using DistilBERT.

    Auto-detects PyTorch or TensorFlow at runtime.  Falls back gracefully if
    neither framework is installed, filling the DL columns with 'neutral'/0.0.

    Returns the same DataFrame for chaining.
    """
    backend = detect_dl_backend()

    if backend == "none":
        warnings.warn(
            "Neither PyTorch nor TensorFlow is installed. "
            "Skipping deep-learning sentiment analysis.",
            RuntimeWarning,
            stacklevel=2,
        )
        df["dl_label"] = "neutral"
        df["dl_confidence"] = 0.0
        df["dl_backend"] = "none"
        return df

    classifier = _load_dl_pipeline(backend)
    texts = df[text_col].tolist()

    # Run inference in batches to avoid OOM on large datasets.
    results: list[dict] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        results.extend(classifier(batch))

    df["dl_label"] = [_dl_label(r["label"]) for r in results]
    df["dl_confidence"] = [round(r["score"], 4) for r in results]
    df["dl_backend"] = backend
    return df


# ── Aggregation – majority vote ───────────────────────────────────────────────

def _majority_vote(row: pd.Series) -> tuple[SentimentLabel, float]:
    """
    Return (final_label, confidence) where confidence = fraction of layers
    that agreed on the winning label.
    """
    labels: list[SentimentLabel] = [
        row["textblob_label"],
        row["vader_label"],
        row["dl_label"],
    ]
    counts: dict[SentimentLabel, int] = {}
    for lbl in labels:
        counts[lbl] = counts.get(lbl, 0) + 1

    winner: SentimentLabel = max(counts, key=lambda k: counts[k])
    confidence = round(counts[winner] / len(labels), 4)
    return winner, confidence


def apply_majority_vote(df: pd.DataFrame) -> pd.DataFrame:
    """Add final_label and final_confidence columns. Returns df for chaining."""
    votes = df.apply(_majority_vote, axis=1)
    df["final_label"] = votes.apply(lambda v: v[0])
    df["final_confidence"] = votes.apply(lambda v: v[1])
    return df


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_tweets(
    tweets: list[TweetRecord],
    use_dl: bool = True,
    dl_batch_size: int = 32,
) -> pd.DataFrame:
    """
    Run the full three-layer sentiment pipeline on a list of TweetRecords.

    Parameters
    ----------
    tweets:
        Raw records from scraper.scrape_profile_tweets().
    use_dl:
        Set to False to skip the DistilBERT layer (faster, less accurate).
    dl_batch_size:
        Number of tweets to send to the DL model per batch.

    Returns
    -------
    pd.DataFrame
        One row per tweet with all raw scores plus final_label / final_confidence.
        Columns mirror the SentimentRecord TypedDict.
    """
    if not tweets:
        return pd.DataFrame()

    df = pd.DataFrame(tweets)

    # Layer 1 – TextBlob
    analyze_textblob(df)

    # Layer 2 – VADER
    analyze_vader(df)

    # Layer 3 – DistilBERT (optional)
    if use_dl:
        analyze_dl(df, batch_size=dl_batch_size)
    else:
        df["dl_label"] = "neutral"
        df["dl_confidence"] = 0.0
        df["dl_backend"] = "none"

    # Aggregation
    apply_majority_vote(df)

    return df


def summarize(df: pd.DataFrame) -> dict:
    """
    Return a plain-dict summary of sentiment distribution for quick inspection.

    Example
    -------
    {
        'total': 50,
        'positive': 30,
        'negative': 12,
        'neutral': 8,
        'positive_pct': 60.0,
        'negative_pct': 24.0,
        'neutral_pct': 16.0,
        'avg_vader_compound': 0.21,
        'avg_textblob_polarity': 0.15,
        'avg_dl_confidence': 0.87,
    }
    """
    if df.empty:
        return {}

    total = len(df)
    counts = df["final_label"].value_counts().to_dict()

    def pct(n: int) -> float:
        return round(n / total * 100, 1)

    positive = counts.get("positive", 0)
    negative = counts.get("negative", 0)
    neutral = counts.get("neutral", 0)

    summary: dict = {
        "total": total,
        "positive": positive,
        "negative": negative,
        "neutral": neutral,
        "positive_pct": pct(positive),
        "negative_pct": pct(negative),
        "neutral_pct": pct(neutral),
        "avg_vader_compound": round(df["vader_compound"].mean(), 4),
        "avg_textblob_polarity": round(df["textblob_polarity"].mean(), 4),
    }

    if (df["dl_backend"] != "none").any():
        summary["avg_dl_confidence"] = round(df["dl_confidence"].mean(), 4)
        summary["dl_backend"] = df["dl_backend"].iloc[0]

    return summary
