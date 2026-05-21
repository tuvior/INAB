from __future__ import annotations

from pathlib import Path

from inab.web import create_app
from inab.config import Settings
from inab.store import Store


def test_database_path_is_backend_scoped_by_default(tmp_path: Path) -> None:
    ynab = Settings(data_dir=tmp_path, backend="ynab")
    actual = Settings(data_dir=tmp_path, backend="actual")

    assert ynab.database_path == tmp_path / "ynab" / "inab.sqlite3"
    assert actual.database_path == tmp_path / "actual" / "inab.sqlite3"


def test_database_path_override_is_explicit(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        backend="actual",
        database_path_override=tmp_path / "custom.sqlite3",
    )

    assert settings.database_path == tmp_path / "custom.sqlite3"


def test_actual_budget_env_seed_is_backend_local(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        backend="actual",
        actual_base_url="https://actual.example",
        actual_password="secret",
        actual_budget="Household",
    )
    store = Store(settings.database_path)

    create_app(
        settings=settings, store=store, gateway_factory=lambda _settings: _NoopGateway()
    )

    assert store.selected_plan() == ("Household", "Household")
    assert settings.database_path == tmp_path / "actual" / "inab.sqlite3"


class _NoopGateway:
    backend_name = "noop"
    backend_label = "Noop"
