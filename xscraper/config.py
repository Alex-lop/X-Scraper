from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    bearer_token_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        runtime_dir = Path(os.environ.get("XSCRAPER_RUNTIME_DIR", "var")).expanduser().resolve()
        return cls(
            database_path=Path(os.environ.get("XSCRAPER_DB_PATH", runtime_dir / "x_api_analyst.db"))
            .expanduser()
            .resolve(),
            bearer_token_path=Path(
                os.environ.get(
                    "XSCRAPER_X_BEARER_TOKEN_PATH", runtime_dir / "auth" / "x_bearer_token"
                )
            )
            .expanduser()
            .resolve(),
        )

    def ensure_runtime_dirs(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.bearer_token_path.parent.mkdir(parents=True, exist_ok=True)

    def bearer_token(self) -> str | None:
        override = os.environ.get("XSCRAPER_X_BEARER_TOKEN", "").strip()
        if override:
            return override
        try:
            token = self.bearer_token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None

    def connection_status(self) -> dict[str, str | bool]:
        token = self.bearer_token()
        source = "environment" if os.environ.get("XSCRAPER_X_BEARER_TOKEN", "").strip() else "file"
        return {
            "status": "configured" if token else "missing",
            "valid": bool(token),
            "source": source if token else "none",
            "message": ("Bearer Token is configured." if token else "Run: xscraper configure"),
        }
