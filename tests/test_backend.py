from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from conftest import FakeGateway, login
from inab.backend import BackendManager
from inab.config import Settings
from inab.store import Store
from inab.web import create_app


def test_backend_manager_persists_active_backend(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, backend="ynab")
    manager = BackendManager(settings)

    manager.switch_backend("actual")
    restored = BackendManager(Settings(data_dir=tmp_path, backend="ynab"))

    assert restored.active_backend == "actual"
    assert restored.settings("ynab").database_path == tmp_path / "ynab" / "inab.sqlite3"
    assert (
        restored.settings("actual").database_path
        == tmp_path / "actual" / "inab.sqlite3"
    )


def test_setup_backend_switch_keeps_state_separate(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        backend="ynab",
        ynab_access_token="fake-token",
        actual_base_url="https://actual.example",
        actual_password="secret",
        username="inab",
        password="secret",
        session_secret="test-session",
    )
    ynab_gateway = FakeGateway()
    actual_gateway = FakeGateway()
    actual_gateway.backend_name = "actual"
    actual_gateway.backend_label = "Actual Budget"
    actual_gateway.plans = [
        actual_gateway.plans[0].__class__(id="actual-plan", name="Imported")
    ]

    def gateway_factory(active_settings: Settings) -> FakeGateway:
        return actual_gateway if active_settings.backend == "actual" else ynab_gateway

    client = TestClient(create_app(settings=settings, gateway_factory=gateway_factory))
    login(client)

    ynab_save = client.post(
        "/setup",
        content="action=self_names&self_names=YNAB+Name",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    switch = client.post(
        "/setup",
        data={"action": "backend", "backend": "actual"},
        follow_redirects=False,
    )
    actual_save = client.post(
        "/setup",
        content="action=self_names&self_names=Actual+Name",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert ynab_save.status_code == 303
    assert switch.status_code == 303
    assert actual_save.status_code == 303
    assert Store(tmp_path / "ynab" / "inab.sqlite3").self_names() == ["YNAB Name"]
    assert Store(tmp_path / "actual" / "inab.sqlite3").self_names() == ["Actual Name"]
