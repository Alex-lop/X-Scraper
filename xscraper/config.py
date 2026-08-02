from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class Settings:
    runtime_dir: Path
    database_path: Path
    storage_state_path: Path
    artifacts_dir: Path
    headless: bool = True
    job_timeout_seconds: int = 600
    page_timeout_ms: int = 60_000
    no_new_page_limit: int = 3
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> Settings:
        runtime_dir = (
            Path(os.environ.get("XSCRAPER_RUNTIME_DIR", PROJECT_ROOT / "var"))
            .expanduser()
            .resolve()
        )
        return cls(
            runtime_dir=runtime_dir,
            database_path=Path(
                os.environ.get("XSCRAPER_DB_PATH", runtime_dir / "twitter_scraper.db")
            )
            .expanduser()
            .resolve(),
            storage_state_path=Path(
                os.environ.get(
                    "XSCRAPER_STORAGE_STATE",
                    runtime_dir / "auth" / "storage_state.json",
                )
            )
            .expanduser()
            .resolve(),
            artifacts_dir=Path(os.environ.get("XSCRAPER_ARTIFACTS_DIR", runtime_dir / "artifacts"))
            .expanduser()
            .resolve(),
            headless=os.environ.get("XSCRAPER_HEADLESS", "1") != "0",
            job_timeout_seconds=int(os.environ.get("XSCRAPER_JOB_TIMEOUT", "600")),
        )

    def ensure_runtime_dirs(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
