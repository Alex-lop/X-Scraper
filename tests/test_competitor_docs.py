from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
LEDGER = (ROOT / "docs" / "competitor-landscape.md").read_text(encoding="utf-8")

PROJECTS = [
    "X-Scraper",
    "Tweepy",
    "node-twitter-api-v2",
    "twarc",
    "Twikit",
    "twscrape",
    "Nitter",
    "snscrape",
    "twitter-scraper-selenium",
    "RSSHub",
    "MediaCrawler",
    "x-crawler",
    "TwitterAccountMediaDownload",
]


def _section(text: str, heading: str) -> str:
    body = text.split(f"## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def test_readme_comparison_shape_matches_the_detailed_ledger():
    capabilities = _section(README, "Capabilities and limits")
    capability_rows = [line for line in capabilities.splitlines() if line.startswith("| ")][2:]
    assert [row.split("|")[1].strip() for row in capability_rows] == [
        "Offline demo",
        "SQLite evidence/Changes/export",
        "Bounded saved-source batch",
        "Browser capture",
        "Official X API",
        "MCP",
        "Live X acceptance",
    ]
    assert all(len(row.split("|")) == 5 for row in capability_rows)

    comparison = _section(README, "How X-Scraper compares")
    rows = [line for line in comparison.splitlines() if line.startswith("| ")]
    assert rows[0].split("|")[1:-1] == [
        " Project ",
        " Product shape ",
        " X surfaces ",
        " Queue/resume ",
        " Storage/output ",
        " Operator UX ",
        " Current evidence ",
        " Candid verdict ",
    ]
    assert len(rows) == len(PROJECTS) + 2
    assert all(len(row.split("|")) == 10 for row in rows)
    readme_projects = [
        re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", row.split("|")[1]).strip()
        for row in rows[2:]
    ]
    assert readme_projects == PROJECTS
    assert re.findall(r"^## (.+)$", LEDGER, flags=re.MULTILINE)[1:-1] == PROJECTS

    required_fields = (
        "Snapshot",
        "Documentation language",
        "Transport class",
        "Claimed and source-present surfaces",
        "Durability",
        "UI",
        "CI evidence",
        "Contradictory evidence",
        "Prohibited inference",
        "Cheapest authorized falsification",
        "Recheck trigger",
        "Verdict",
    )
    for index, project in enumerate(PROJECTS):
        start = LEDGER.index(f"## {project}\n")
        next_heading = (
            LEDGER.find("\n## ", start + 4)
            if index < len(PROJECTS) - 1
            else LEDGER.index("\n## Safe takeaways", start)
        )
        project_section = LEDGER[start:next_heading]
        assert all(f"**{field}" in project_section for field in required_fields), project


def test_comparison_mermaid_and_evidence_links_are_bounded_and_immutable():
    comparison = _section(README, "How X-Scraper compares")
    mermaid = comparison.split("```mermaid\n", 1)[1].split("\n```", 1)[0]
    assert len(re.findall(r'^    [A-Z]+\["', mermaid, flags=re.MULTILINE)) == 20
    assert len(re.findall(r"^    [A-Z]+ --> [A-Z]+$", mermaid, flags=re.MULTILINE)) == 19
    assert 'ROOT["Curated comparison: X-Scraper plus 12 projects"]' in mermaid

    mutable_source = re.compile(r"github\.com/[^/]+/[^/]+/(?:blob|tree)/(?![0-9a-f]{40}(?:/|\b))")
    assert not mutable_source.search(LEDGER)
    assert "2026-08-20" in LEDGER
    assert all(grade in LEDGER for grade in (
        "REPO FACT",
        "SOURCE PRESENT",
        "PROJECT CI",
        "UPSTREAM-DOCUMENTED",
        "CONTRADICTED",
        "LIVE NOT RUN",
    ))
    assert not re.search(r"(?:Bearer|auth_token|ct0)\s*[=:]\s*[A-Za-z0-9_-]{16,}", LEDGER)


def test_markdown_relative_links_resolve():
    paths = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *(ROOT / "docs").rglob("*.md")]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"(?<!!)\[[^]]+]\(([^)]+)\)", text):
            if "://" in target or target.startswith("#"):
                continue
            relative = unquote(target.split("#", 1)[0])
            assert (path.parent / relative).resolve().exists(), (path, target)
