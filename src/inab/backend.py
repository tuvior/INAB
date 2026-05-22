from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .store import Store

SUPPORTED_BACKENDS = ("ynab", "actual")


class AppConfigStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                create table if not exists app_config (
                    key text primary key,
                    value text not null
                )
                """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def get(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "select value from app_config where key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else None

    def set(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                insert into app_config (key, value)
                values (?, ?)
                on conflict(key) do update set value = excluded.value
                """,
                (key, value),
            )
            connection.commit()


class BackendManager:
    def __init__(
        self,
        settings: Settings,
        *,
        store_override: Store | None = None,
        gateway_factory: Callable[[Settings], Any] | None = None,
    ):
        self.base_settings = settings
        self.gateway_factory = gateway_factory
        self._store_override = store_override
        self._stores: dict[str, Store] = {}
        self._global_config = AppConfigStore(settings.data_dir / "app.sqlite3")
        active_backend = self._global_config.get("active_backend")
        if active_backend not in SUPPORTED_BACKENDS:
            self._global_config.set("active_backend", _valid_backend(settings.backend))

    @property
    def active_backend(self) -> str:
        return _valid_backend(
            self._global_config.get("active_backend") or self.base_settings.backend
        )

    @property
    def backend_options(self) -> list[dict[str, str]]:
        return [
            {"name": "ynab", "label": "YNAB"},
            {"name": "actual", "label": "Actual Budget"},
        ]

    def switch_backend(self, backend: str) -> None:
        backend = _valid_backend(backend)
        self._global_config.set("active_backend", backend)

    def settings(self, backend: str | None = None) -> Settings:
        selected = _valid_backend(backend or self.active_backend)
        return replace(self.base_settings, backend=selected)

    def store(self, backend: str | None = None) -> Store:
        selected = _valid_backend(backend or self.active_backend)
        if self._store_override is not None and selected == _valid_backend(
            self.base_settings.backend
        ):
            return self._store_override
        if selected not in self._stores:
            self._stores[selected] = Store(self.settings(selected).database_path)
        return self._stores[selected]

    def gateway(self, backend: str | None = None) -> Any:
        if self.gateway_factory is None:
            raise RuntimeError("No gateway factory is configured.")
        return self.gateway_factory(self.settings(backend))


class ActiveSettings:
    def __init__(self, manager: BackendManager):
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager.settings(), name)


class ActiveStore:
    def __init__(self, manager: BackendManager):
        self._manager = manager

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager.store(), name)


def _valid_backend(value: str) -> str:
    backend = (value or "ynab").strip().lower()
    if backend not in SUPPORTED_BACKENDS:
        return "ynab"
    return backend
