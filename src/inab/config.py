from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    backend: str = "ynab"
    ynab_access_token: str | None = None
    actual_base_url: str | None = None
    actual_password: str | None = None
    actual_budget: str | None = None
    actual_encryption_password: str | None = None
    actual_data_dir: Path | None = None
    actual_verify_ssl: bool | str = True
    database_path_override: Path | None = None
    username: str | None = None
    password: str | None = None
    session_secret: str = "inab-dev-session-secret"
    self_names: tuple[str, ...] = ()
    root_path: str = ""
    max_upload_bytes: int = 10 * 1024 * 1024
    target_currency: str = "CHF"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("INAB_DATA_DIR", "data")).expanduser()
        backend = os.environ.get("INAB_BACKEND", "ynab").strip().lower() or "ynab"
        database_path_override = os.environ.get("INAB_DATABASE_PATH")
        actual_data_dir = os.environ.get("ACTUAL_DATA_DIR")
        username = os.environ.get("INAB_USERNAME")
        password = os.environ.get("INAB_PASSWORD")
        session_secret = (
            os.environ.get("INAB_SESSION_SECRET")
            or password
            or "inab-dev-session-secret"
        )
        max_upload_bytes = int(
            os.environ.get("INAB_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))
        )
        return cls(
            data_dir=data_dir,
            backend=backend,
            ynab_access_token=os.environ.get("YNAB_ACCESS_TOKEN"),
            actual_base_url=os.environ.get("ACTUAL_BASE_URL"),
            actual_password=os.environ.get("ACTUAL_PASSWORD"),
            actual_budget=os.environ.get("ACTUAL_BUDGET"),
            actual_encryption_password=os.environ.get("ACTUAL_ENCRYPTION_PASSWORD")
            or None,
            actual_data_dir=(
                Path(actual_data_dir).expanduser() if actual_data_dir else None
            ),
            actual_verify_ssl=_parse_verify_ssl(os.environ.get("ACTUAL_VERIFY_SSL")),
            database_path_override=(
                Path(database_path_override).expanduser()
                if database_path_override
                else None
            ),
            username=username,
            password=password,
            session_secret=session_secret,
            self_names=_parse_csv(os.environ.get("INAB_SELF_NAMES")),
            root_path=_normalize_root_path(os.environ.get("INAB_ROOT_PATH")),
            max_upload_bytes=max_upload_bytes,
            target_currency=os.environ.get("INAB_TARGET_CURRENCY", "CHF").upper(),
        )

    @property
    def auth_configured(self) -> bool:
        return bool(self.username and self.password)

    @property
    def ynab_configured(self) -> bool:
        return bool(self.ynab_access_token)

    @property
    def actual_configured(self) -> bool:
        return bool(
            self.actual_base_url and self.actual_password and self.actual_budget
        )

    @property
    def backend_configured(self) -> bool:
        if self.backend == "ynab":
            return self.ynab_configured
        if self.backend == "actual":
            return self.actual_configured
        return False

    @property
    def backend_label(self) -> str:
        if self.backend == "actual":
            return "Actual Budget"
        if self.backend == "ynab":
            return "YNAB"
        return self.backend

    @property
    def database_path(self) -> Path:
        if self.database_path_override:
            return self.database_path_override
        return self.data_dir / self.backend / "inab.sqlite3"


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _normalize_root_path(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip().strip("/")
    return f"/{normalized}" if normalized else ""


def _parse_verify_ssl(value: str | None) -> bool | str:
    if value is None or not value.strip():
        return True
    normalized = value.strip().casefold()
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    return value.strip()
