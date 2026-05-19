from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ynab_access_token: str | None
    username: str | None
    password: str | None
    session_secret: str
    self_names: tuple[str, ...] = ()
    root_path: str = ""
    max_upload_bytes: int = 10 * 1024 * 1024
    target_currency: str = "CHF"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("INAB_DATA_DIR", "data")).expanduser()
        username = os.environ.get("INAB_USERNAME")
        password = os.environ.get("INAB_PASSWORD")
        session_secret = os.environ.get("INAB_SESSION_SECRET") or password or "inab-dev-session-secret"
        max_upload_bytes = int(os.environ.get("INAB_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
        return cls(
            data_dir=data_dir,
            ynab_access_token=os.environ.get("YNAB_ACCESS_TOKEN"),
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
    def database_path(self) -> Path:
        return self.data_dir / "inab.sqlite3"


def _parse_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _normalize_root_path(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.strip().strip("/")
    return f"/{normalized}" if normalized else ""
