from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from inab.store import Store
from inab.web import _save_plan, format_money
from inab.ynab_api import YnabPlan

from conftest import FakeGateway, camt_document, entry_xml, login, statement_xml


def test_auth_gate_redirects_to_login(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, _, _ = app_client

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_upload_preview_marks_existing_import_id_as_duplicate(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(iban="CH111", ynab_account_id="checking-id", ynab_account_name="Checking", transfer_payee_id="tp-checking")
    gateway.existing[("plan-1", "checking-id")] = {"INAB:REF1"}
    content = camt_document(statement_xml("CH111", entry_xml("10.00", "DBIT", "REF1", "Payee"), opening="100.00", closing="90.00"))

    response = client.post("/uploads", files={"file": ("test.xml", content, "application/xml")}, follow_redirects=True)

    assert response.status_code == 200
    assert "duplicate" in response.text
    assert "INAB:REF1" in response.text


def test_import_accepts_transfer_and_skips_counterpart(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(iban="CH111", ynab_account_id="checking-id", ynab_account_name="Checking", transfer_payee_id="tp-checking")
    store.upsert_mapping(iban="CH222", ynab_account_id="savings-id", ynab_account_name="Savings", transfer_payee_id="tp-savings")
    content = camt_document(
        statement_xml("CH111", entry_xml("250.00", "DBIT", "REF-D", "Transfer to savings"), opening="1000.00", closing="750.00")
        + statement_xml("CH222", entry_xml("250.00", "CRDT", "REF-C", "Transfer from checking"), opening="500.00", closing="750.00")
    )

    upload = client.post("/uploads", files={"file": ("multi.xml", content, "application/xml")}, follow_redirects=False)
    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    transfer_id = job["payload"]["transfers"][0]["id"]

    response = client.post(f"/imports/{job_id}", data={"accepted_transfers": transfer_id}, follow_redirects=False)

    assert response.status_code == 303
    assert len(gateway.created) == 1
    assert gateway.created[0]["account_id"] == "checking-id"
    assert gateway.created[0]["payee_id"] == "tp-savings"
    assert gateway.created[0]["amount"] == -250000
    updated_job = store.get_job(job_id)
    assert updated_job is not None
    assert updated_job["result"]["skipped_transfer_counterparts"] == ["INAB:REF-C"]


def test_empty_statement_iban_does_not_block_preview(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(iban="CH111", ynab_account_id="checking-id", ynab_account_name="Checking", transfer_payee_id="tp-checking")
    content = camt_document(
        statement_xml("CH111", entry_xml("10.00", "DBIT", "REF1", "Achat TWINT SBB MOBILE"), opening="100.00", closing="90.00")
        + statement_xml("CH222", "", opening="500.00", closing="500.00")
    )

    upload = client.post("/uploads", files={"file": ("multi.xml", content, "application/xml")}, follow_redirects=False)

    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    assert job["status"] == "preview"
    assert job["payload"]["missing_ibans"] == []


def test_save_plan_accepts_uuid_like_ids(tmp_path) -> None:
    store = Store(tmp_path / "inab.sqlite3")
    gateway = FakeGateway()
    gateway.plans = [YnabPlan(id=UUID("12345678-1234-5678-1234-567812345678"), name="Household")]  # type: ignore[arg-type]

    _save_plan(store, gateway, "12345678-1234-5678-1234-567812345678")

    assert store.selected_plan() == ("12345678-1234-5678-1234-567812345678", "Household")


def test_format_money_uses_two_decimals_and_swiss_grouping() -> None:
    assert format_money("-1.5") == "-1.50"
    assert format_money("-1500") == "-1'500.00"
    assert format_money("8984") == "8'984.00"
