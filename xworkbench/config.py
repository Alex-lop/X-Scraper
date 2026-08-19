from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_CONFIG_BYTES = 64 * 1024
MAX_TOKEN_LENGTH = 4096


class SettingsError(ValueError):
    """An actionable local configuration error."""


def _absolute_path(value: str | os.PathLike[str], name: str) -> Path:
    try:
        text = os.fspath(value)
    except TypeError as exc:
        raise SettingsError(f"{name} must be a filesystem path.") from exc
    if not isinstance(text, str) or not text or "\0" in text:
        raise SettingsError(f"{name} must be a non-empty filesystem path.")
    return Path(os.path.abspath(os.path.expanduser(text)))


def _private_regular_file(path: Path, label: str) -> os.stat_result | None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SettingsError(f"Cannot inspect {label} at {path}: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise SettingsError(f"{label} must be a regular file, not a symlink: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) != 0o600:
        raise SettingsError(f"{label} must have permissions 0600. Run: chmod 600 {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise SettingsError(f"{label} must be owned by the current user: {path}")
    return details


def validate_token(value: str) -> str:
    token = value[:-1] if value.endswith("\n") else value
    if (
        not token
        or len(token) > MAX_TOKEN_LENGTH
        or token != token.strip()
        or "\n" in token
        or "\r" in token
        or not token.isprintable()
    ):
        raise SettingsError(
            f"Bearer Token must be one printable line of 1-{MAX_TOKEN_LENGTH} characters."
        )
    return token


def _private_directory(path: Path) -> None:
    missing = []
    current = path
    while True:
        try:
            current.lstat()
            break
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent

    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            directory.chmod(0o700)

    try:
        details = path.lstat()
    except OSError as exc:
        raise SettingsError(f"Cannot inspect app directory {path}: {exc}") from exc
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
        raise SettingsError(f"App directory must be a regular directory: {path}")
    if os.name != "nt" and stat.S_IMODE(details.st_mode) != 0o700:
        raise SettingsError(f"App directory must have permissions 0700: {path}")
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise SettingsError(f"App directory must be owned by the current user: {path}")


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
    max_workers: int = 1
    queue_capacity: int = 100
    retention_keep_per_source: int = 10
    snapshot_stale_after_seconds: int = 86_400
    config_path: Path | None = None

    def __post_init__(self) -> None:
        database_path = _absolute_path(self.database_path, "database_path")
        bearer_token_path = _absolute_path(self.bearer_token_path, "bearer_token_path")
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "bearer_token_path", bearer_token_path)
        object.__setattr__(
            self,
            "storage_state_path",
            _absolute_path(
                self.storage_state_path
                if self.storage_state_path is not None
                else database_path.parent / "auth" / "playwright_state.json",
                "storage_state_path",
            ),
        )
        object.__setattr__(
            self,
            "config_path",
            _absolute_path(
                self.config_path
                if self.config_path is not None
                else database_path.parent / "config.json",
                "config_path",
            ),
        )
        auth_directory = Path(self.config_path).parent / "auth"
        if Path(self.storage_state_path).parent != auth_directory:
            raise SettingsError(
                "storage_state_path must be inside the app-owned auth directory: "
                f"{auth_directory}"
            )
        if not isinstance(self.browser_headless, bool):
            raise SettingsError("browser_headless must be true or false.")
        for name, value, minimum, maximum in (
            ("job_timeout_seconds", self.job_timeout_seconds, 1, 3600),
            ("page_timeout_ms", self.page_timeout_ms, 100, 300_000),
            ("no_progress_limit", self.no_progress_limit, 1, 100),
            ("max_workers", self.max_workers, 1, 2),
            ("queue_capacity", self.queue_capacity, 1, 10_000),
            ("retention_keep_per_source", self.retention_keep_per_source, 1, 100),
            (
                "snapshot_stale_after_seconds",
                self.snapshot_stale_after_seconds,
                0,
                315_360_000,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise SettingsError(f"{name} must be an integer from {minimum} to {maximum}.")

    @classmethod
    def from_env(cls) -> Settings:
        runtime_dir = _absolute_path(os.environ.get("XWORKBENCH_RUNTIME_DIR", "var"), "runtime")
        config_path = _absolute_path(
            os.environ.get("XWORKBENCH_CONFIG_PATH", runtime_dir / "config.json"),
            "XWORKBENCH_CONFIG_PATH",
        )
        configured = cls._read_config(config_path)

        def setting(name: str, default: Any) -> Any:
            environment = f"XWORKBENCH_{name.upper()}"
            return os.environ.get(environment, configured.get(name, default))

        return cls(
            database_path=_absolute_path(
                setting("db_path", runtime_dir / "x_collection_workbench.db"), "database_path"
            ),
            bearer_token_path=_absolute_path(
                setting("x_bearer_token_path", runtime_dir / "auth" / "x_bearer_token"),
                "bearer_token_path",
            ),
            storage_state_path=_absolute_path(
                setting("storage_state_path", runtime_dir / "auth" / "playwright_state.json"),
                "storage_state_path",
            ),
            browser_headless=cls._boolean(setting("browser_headless", False), "browser_headless"),
            job_timeout_seconds=cls._integer(
                setting("job_timeout_seconds", 120), "job_timeout_seconds"
            ),
            page_timeout_ms=cls._integer(setting("page_timeout_ms", 30_000), "page_timeout_ms"),
            no_progress_limit=cls._integer(
                setting("no_progress_limit", 3), "no_progress_limit"
            ),
            max_workers=cls._integer(setting("max_workers", 1), "max_workers"),
            queue_capacity=cls._integer(
                setting("queue_capacity", 100), "queue_capacity"
            ),
            retention_keep_per_source=cls._integer(
                setting("retention_keep_per_source", 10),
                "retention_keep_per_source",
            ),
            snapshot_stale_after_seconds=cls._integer(
                setting("snapshot_stale_after_seconds", 86_400),
                "snapshot_stale_after_seconds",
            ),
            config_path=config_path,
        )

    @staticmethod
    def _read_config(path: Path) -> dict[str, Any]:
        details = _private_regular_file(path, "Config file")
        if details is None:
            return {}
        if details.st_size > MAX_CONFIG_BYTES:
            raise SettingsError(
                f"Config file is too large (maximum {MAX_CONFIG_BYTES} bytes): {path}"
            )
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SettingsError(f"Config file is not valid UTF-8 JSON: {path}") from exc
        if not isinstance(value, dict):
            raise SettingsError(f"Config file must contain one JSON object: {path}")
        allowed = {
            "db_path",
            "x_bearer_token_path",
            "storage_state_path",
            "browser_headless",
            "job_timeout_seconds",
            "page_timeout_ms",
            "no_progress_limit",
            "max_workers",
            "queue_capacity",
            "retention_keep_per_source",
            "snapshot_stale_after_seconds",
        }
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SettingsError(f"Unknown config key(s): {', '.join(unknown)}")
        return value

    @staticmethod
    def _boolean(value: Any, name: str) -> bool:
        if isinstance(value, bool):
            return value
        if value in {"0", "1"}:
            return value == "1"
        raise SettingsError(f"{name} must be true/false in config or 0/1 in the environment.")

    @staticmethod
    def _integer(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise SettingsError(f"{name} must be an integer.")
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{name} must be an integer; received {value!r}.") from exc

    def ensure_runtime_dirs(self) -> None:
        paths = {
            self.database_path.parent,
            self.bearer_token_path.parent,
            Path(self.storage_state_path).parent,
            Path(self.config_path).parent,
        }
        for path in sorted(paths, key=lambda item: len(item.parts)):
            _private_directory(path)

    def ensure_config_file(self) -> None:
        path = Path(self.config_path)
        if _private_regular_file(path, "Config file") is not None:
            return
        content = json.dumps(
            {
                "browser_headless": self.browser_headless,
                "job_timeout_seconds": self.job_timeout_seconds,
                "page_timeout_ms": self.page_timeout_ms,
                "no_progress_limit": self.no_progress_limit,
                "max_workers": self.max_workers,
                "queue_capacity": self.queue_capacity,
                "retention_keep_per_source": self.retention_keep_per_source,
                "snapshot_stale_after_seconds": self.snapshot_stale_after_seconds,
            },
            indent=2,
        )
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                target.write(content + "\n")
            path.chmod(0o600)
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def validate_local_files(self) -> None:
        _private_regular_file(Path(self.config_path), "Config file")
        _private_regular_file(self.database_path, "Database file")
        _private_regular_file(self.bearer_token_path, "Bearer Token file")
        _private_regular_file(Path(self.storage_state_path), "Browser auth state")
        state_path = Path(self.storage_state_path)
        _private_regular_file(
            state_path.with_name(f".{state_path.name}.auth-status"),
            "Browser auth status",
        )
        for path in {
            self.database_path.parent,
            self.bearer_token_path.parent,
            state_path.parent,
            Path(self.config_path).parent,
        }:
            try:
                details = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
                raise SettingsError(f"App directory must be a regular directory: {path}")
            if os.name != "nt" and stat.S_IMODE(details.st_mode) != 0o700:
                raise SettingsError(
                    f"App directory must have permissions 0700. Run: chmod 700 {path}"
                )

    def public_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "database_path": str(self.database_path),
            "bearer_token_path": str(self.bearer_token_path),
            "storage_state_path": str(self.storage_state_path),
            "browser_headless": self.browser_headless,
            "job_timeout_seconds": self.job_timeout_seconds,
            "page_timeout_ms": self.page_timeout_ms,
            "no_progress_limit": self.no_progress_limit,
            "max_workers": self.max_workers,
            "queue_capacity": self.queue_capacity,
            "retention_keep_per_source": self.retention_keep_per_source,
            "snapshot_stale_after_seconds": self.snapshot_stale_after_seconds,
            "per_source_concurrency": 1,
            "per_auth_state_concurrency": 1,
            "hard_worker_maximum": 4,
            "route_mode": "direct",
        }

    def bearer_token(self) -> str | None:
        override = (
            os.environ.get("XWORKBENCH_X_BEARER_TOKEN", "")
            if self.allow_environment_token
            else ""
        )
        if override:
            return validate_token(override)
        if _private_regular_file(self.bearer_token_path, "Bearer Token file") is None:
            return None
        try:
            return validate_token(self.bearer_token_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            raise SettingsError(f"Cannot read Bearer Token file: {self.bearer_token_path}") from exc

    def connection_status(self) -> dict[str, str | bool]:
        try:
            token = self.bearer_token()
        except SettingsError as exc:
            return {
                "status": "invalid",
                "valid": False,
                "source": "none",
                "message": str(exc),
            }
        source = (
            "environment"
            if self.allow_environment_token and os.environ.get("XWORKBENCH_X_BEARER_TOKEN", "")
            else "file"
        )
        return {
            "status": "configured" if token else "missing",
            "valid": bool(token),
            "source": source if token else "none",
            "message": ("Bearer Token is configured." if token else "Run: xworkbench configure"),
        }
