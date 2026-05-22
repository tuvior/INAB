from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
TARGET_CURRENCY = "CHF"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    backend: str = "ynab"
    ynab_access_token: str | None = None
    actual_base_url: str | None = None
    actual_password: str | None = None
    actual_encryption_password: str | None = None
    actual_data_dir: Path | None = None
    actual_verify_ssl: bool | str = True
    username: str | None = None
    password: str | None = None
    session_secret: str = "inab-dev-session-secret"
    root_path: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("INAB_DATA_DIR", "data")).expanduser()
        actual_data_dir = os.environ.get("ACTUAL_DATA_DIR")
        username = os.environ.get("INAB_USERNAME")
        password = os.environ.get("INAB_PASSWORD")
        session_secret = (
            os.environ.get("INAB_SESSION_SECRET")
            or password
            or "inab-dev-session-secret"
        )
        return cls(
            data_dir=data_dir,
            ynab_access_token=os.environ.get("YNAB_ACCESS_TOKEN"),
            actual_base_url=os.environ.get("ACTUAL_BASE_URL"),
            actual_password=os.environ.get("ACTUAL_PASSWORD"),
            actual_encryption_password=os.environ.get("ACTUAL_ENCRYPTION_PASSWORD")
            or None,
            actual_data_dir=(
                Path(actual_data_dir).expanduser() if actual_data_dir else None
            ),
            actual_verify_ssl=_parse_verify_ssl(os.environ.get("ACTUAL_VERIFY_SSL")),
            username=username,
            password=password,
            session_secret=session_secret,
            root_path=_normalize_root_path(os.environ.get("INAB_ROOT_PATH")),
        )

    @property
    def auth_configured(self) -> bool:
        return bool(self.username and self.password)

    @property
    def ynab_configured(self) -> bool:
        return bool(self.ynab_access_token)

    @property
    def actual_configured(self) -> bool:
        return bool(self.actual_base_url and self.actual_password)

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
        return self.data_dir / self.backend / "inab.sqlite3"


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
