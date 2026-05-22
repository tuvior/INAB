from __future__ import annotations

from pathlib import Path

from inab.config import Settings


def test_database_path_is_backend_scoped_by_default(tmp_path: Path) -> None:
    ynab = Settings(data_dir=tmp_path, backend="ynab")
    actual = Settings(data_dir=tmp_path, backend="actual")

    assert ynab.database_path == tmp_path / "ynab" / "inab.sqlite3"
    assert actual.database_path == tmp_path / "actual" / "inab.sqlite3"


def test_actual_configuration_does_not_require_budget_env(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        backend="actual",
        actual_base_url="https://actual.example",
        actual_password="secret",
    )

    assert settings.actual_configured is True
    assert settings.database_path == tmp_path / "actual" / "inab.sqlite3"
