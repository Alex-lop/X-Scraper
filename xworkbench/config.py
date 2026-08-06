from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    bearer_token_path: Path
    allow_environment_token: bool = True
    storage_state_path: Path | None = None
    browser_headless: bool = False
    job_timeout_seconds: int = 120
    page_timeout_ms: int = 30_000
    no_progress_limit: int = 3

    def __post_init__(self) -> None:
        runtime_dir = self.database_path.parent
        if self.storage_state_path is None:
            object.__setattr__(
                self, "storage_state_path", runtime_dir / "auth" / "playwright_state.json"
            )

    @classmethod
    def from_env(cls) -> Settings:
        runtime_dir = Path(os.environ.get("XWORKBENCH_RUNTIME_DIR", "var")).expanduser().resolve()
        return cls(
            database_path=Path(
                os.environ.get("XWORKBENCH_DB_PATH", runtime_dir / "x_collection_workbench.db")
            )
            .expanduser()
            .resolve(),
            bearer_token_path=Path(
                os.environ.get(
                    "XWORKBENCH_X_BEARER_TOKEN_PATH", runtime_dir / "auth" / "x_bearer_token"
                )
            )
            .expanduser()
            .resolve(),
            storage_state_path=Path(
                os.environ.get(
                    "XWORKBENCH_STORAGE_STATE_PATH",
                    runtime_dir / "auth" / "playwright_state.json",
                )
            )
            .expanduser()
            .resolve(),
            browser_headless=os.environ.get("XWORKBENCH_BROWSER_HEADLESS", "0") == "1",
            job_timeout_seconds=int(os.environ.get("XWORKBENCH_JOB_TIMEOUT_SECONDS", "120")),
            page_timeout_ms=int(os.environ.get("XWORKBENCH_PAGE_TIMEOUT_MS", "30000")),
            no_progress_limit=int(os.environ.get("XWORKBENCH_NO_PROGRESS_LIMIT", "3")),
        )

    def ensure_runtime_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.bearer_token_path.parent.mkdir(parents=True, exist_ok=True)
        assert self.storage_state_path is not None
        self.storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    def bearer_token(self) -> str | None:
        override = (
            os.environ.get("XWORKBENCH_X_BEARER_TOKEN", "").strip()
            if self.allow_environment_token
            else ""
        )
        if override:
            return override
        try:
            token = self.bearer_token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    def connection_status(self) -> dict[str, str | bool]:
        token = self.bearer_token()
        source = (
            "environment"
            if self.allow_environment_token
            and os.environ.get("XWORKBENCH_X_BEARER_TOKEN", "").strip()
            else "file"
        )
        return {
            "status": "configured" if token else "missing",
            "valid": bool(token),
            "source": source if token else "none",
            "message": ("Bearer Token is configured." if token else "Run: xworkbench configure"),
        }
