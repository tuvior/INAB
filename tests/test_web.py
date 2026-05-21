from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from fastapi.testclient import TestClient

from inab.config import Settings
from inab.models import make_import_id
from inab.store import Store
from inab.web import _save_plan, create_app, format_money
from inab.ynab_api import ExistingTransaction, YnabPlan

from conftest import FakeGateway, camt_document, entry_xml, login, statement_xml


def test_auth_gate_redirects_to_login(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, _, _ = app_client

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_rules_page_requires_auth(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, _, _ = app_client

    response = client.get("/rules", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_import_history_requires_auth(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, _, _ = app_client

    response = client.get("/imports", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_root_path_uses_relative_prefixed_template_urls(
    tmp_path, fake_gateway: FakeGateway
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        ynab_access_token="fake-token",
        username="inab",
        password="secret",
        session_secret="test-session",
        root_path="/inab",
    )
    app = create_app(
        settings=settings,
        store=Store(settings.database_path),
        gateway_factory=lambda _settings: fake_gateway,
    )
    client = TestClient(app)

    response = client.get("/login")

    assert response.status_code == 200
    assert 'href="/inab/static/app.css"' in response.text
    assert 'src="/inab/static/app.js"' in response.text
    assert 'href="/inab/"' in response.text
    assert 'href="/inab/rules"' in response.text
    assert 'href="/inab/setup"' in response.text
    assert 'action="/inab/login"' in response.text
    assert "http://testserver" not in response.text


def test_root_path_redirects_to_prefixed_paths(
    tmp_path, fake_gateway: FakeGateway
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        ynab_access_token="fake-token",
        username="inab",
        password="secret",
        session_secret="test-session",
        root_path="/inab",
    )
    app = create_app(
        settings=settings,
        store=Store(settings.database_path),
        gateway_factory=lambda _settings: fake_gateway,
    )
    client = TestClient(app)

    gated = client.get("/", follow_redirects=False)
    login_response = client.post(
        "/login",
        data={"username": "inab", "password": "secret", "next": "/setup"},
        follow_redirects=False,
    )
    already_prefixed = client.post(
        "/login",
        data={"username": "inab", "password": "secret", "next": "/inab/setup"},
        follow_redirects=False,
    )

    assert gated.status_code == 303
    assert gated.headers["location"] == "/inab/login?next=/"
    assert login_response.status_code == 303
    assert login_response.headers["location"] == "/inab/setup"
    assert already_prefixed.status_code == 303
    assert already_prefixed.headers["location"] == "/inab/setup"


def test_upload_preview_button_is_contextual(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, _, _ = app_client
    login(client)

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="preview-button"' in response.text
    assert "Choose file to preview" in response.text
    assert (
        '<button id="preview-button" class="drop-action" type="submit" disabled>'
        in response.text
    )


def test_rules_page_loads_visible_categories_and_saves_rule(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")

    page = client.get("/rules")

    assert page.status_code == 200
    assert "Everyday: Food" in page.text
    assert "Old: Hidden" not in page.text
    assert "Old: Deleted" not in page.text

    response = client.post(
        "/rules",
        data={
            "action": "create_rule",
            "name": "Coop food",
            "enabled": "1",
            "operator": "contains",
            "pattern": "coop",
            "replacement_payee": "Coop",
            "category": "cat-food\tEveryday: Food",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    rule = store.list_rules()[0]
    assert rule.name == "Coop food"
    assert rule.category_id == "cat-food"
    assert rule.category_name == "Everyday: Food"


def test_rules_page_lists_payees_and_rule_effects(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.create_rule(
        name="Coop cleanup",
        enabled=True,
        operator="contains",
        pattern="coop",
        replacement_payee="Coop",
        category_id="cat-food",
        category_name="Everyday: Food",
    )

    page = client.get("/rules")

    assert page.status_code == 200
    assert "Coop Pronto" in page.text
    assert "Coop cleanup" in page.text
    assert "Everyday: Food" in page.text
    assert "SBB Mobile" in page.text
    assert "No match" in page.text
    assert "Transfer : Savings" in page.text
    assert "Transfer payee" in page.text
    assert "Deleted Payee" not in page.text


def test_rules_page_tests_fake_transaction_first_match_wins(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.create_rule(
        name="First Coop",
        enabled=True,
        operator="contains",
        pattern="coop",
        replacement_payee="Coop",
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    store.create_rule(
        name="Second Coop",
        enabled=True,
        operator="contains",
        pattern="coop",
        replacement_payee="Wrong",
        category_id=None,
        category_name=None,
    )

    response = client.post(
        "/rules",
        data={
            "action": "test_rule",
            "test_payee": "Coop Pronto",
            "test_memo": "Fuel",
        },
    )

    assert response.status_code == 200
    result = response.text.split("would produce", 1)[0].rsplit("rule-test-result", 1)[1]
    assert "First Coop" in result
    assert "Second Coop" not in result
    assert "Wrong" not in result
    assert "Everyday: Food" in response.text


def test_rules_page_displays_invalid_rule_errors(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")

    response = client.post(
        "/rules",
        data={
            "action": "create_rule",
            "name": "Bad",
            "enabled": "1",
            "operator": "regex",
            "pattern": "[",
            "replacement_payee": "Payee",
            "category": "",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Invalid regular expression" in response.text


def test_upload_preview_marks_existing_import_id_as_duplicate(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    gateway.existing[("plan-1", "checking-id")] = {"INAB:REF1"}
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Payee"),
            opening="100.00",
            closing="90.00",
        )
    )

    response = client.post(
        "/uploads",
        files={"file": ("test.xml", content, "application/xml")},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "duplicate" in response.text
    assert "INAB:REF1" in response.text
    assert "Already imported" in response.text
    assert 'title="import_id matched INAB:REF1"' in response.text
    assert gateway.existing_calls == [("plan-1", "checking-id", date(2026, 4, 10))]


def test_upload_preview_marks_bank_date_card_fallback_import_id_as_duplicate(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    old_import_id = make_import_id(
        iban="CH111",
        source_ref=None,
        booking_date=date(2026, 1, 19),
        amount=Decimal("-34.30"),
        payee="Sample Bistro",
        memo="Card: 15.01.2026 13:58",
    )
    gateway.existing[("plan-1", "checking-id")] = {old_import_id}
    entry = """
<Ntry>
  <Amt Ccy="CHF">34.30</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-01-19</Dt></BookgDt>
  <ValDt><Dt>2026-01-19</Dt></ValDt>
  <AddtlNtryInf>Achat Sample Bistro
15.01.2026, 13:58, No carte Visa Debit 400000xxxxxx0002</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="100.00", closing="65.70")
    )

    upload = client.post(
        "/uploads",
        files={"file": ("card.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    row = job["payload"]["rows"][0]
    assert row["transaction"]["booking_date"] == "2026-01-15"
    assert row["status"] == "duplicate"
    assert gateway.existing_calls == [("plan-1", "checking-id", date(2026, 1, 15))]


def test_upload_preview_sorts_rows_by_effective_transaction_date(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    card_entry = """
<Ntry>
  <Amt Ccy="CHF">58.65</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-04-01</Dt></BookgDt>
  <ValDt><Dt>2026-04-01</Dt></ValDt>
  <AcctSvcrRef>CARDREF</AcctSvcrRef>
  <AddtlNtryInf>Achat Coop
30.03.2026, 14:45, No carte Visa Debit 400000xxxxxx0002</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml(
                "5.80",
                "DBIT",
                "TWINTREF",
                "Achat TWINT SBB MOBILE",
                booking_date="2026-03-31",
                value_date="2026-03-31",
            )
            + entry_xml(
                "58.95",
                "DBIT",
                "BILLREF",
                "Insurance Example AG",
                booking_date="2026-04-01",
                value_date="2026-04-01",
            )
            + card_entry,
            opening="100.00",
            closing="-23.40",
        )
    )

    upload = client.post(
        "/uploads",
        files={"file": ("ordered.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    assert [
        (row["transaction"]["booking_date"], row["transaction"]["payee"])
        for row in job["payload"]["rows"]
    ] == [
        ("2026-03-30", "Coop"),
        ("2026-03-31", "SBB Mobile"),
        ("2026-04-01", "Insurance Example AG"),
    ]


def test_upload_preview_applies_rule_metadata_and_import_sends_category(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    store.create_rule(
        name="SBB travel",
        enabled=True,
        operator="contains",
        pattern="sbb",
        replacement_payee="SBB",
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Achat TWINT SBB MOBILE"),
            opening="100.00",
            closing="90.00",
        )
    )

    upload = client.post(
        "/uploads",
        files={"file": ("test.xml", content, "application/xml")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    row = job["payload"]["rows"][0]
    assert row["transaction"]["payee"] == "SBB"
    assert row["transaction"]["original_payee"] == "SBB Mobile"
    assert row["transaction"]["category_id"] == "cat-food"
    assert row["transaction"]["category_name"] == "Everyday: Food"
    assert row["transaction"]["applied_rule_name"] == "SBB travel"

    response = client.post(f"/imports/{job_id}", follow_redirects=False)

    assert response.status_code == 303
    assert gateway.created[0].payee_name == "SBB"
    assert gateway.created[0].category_id == "cat-food"


def test_import_accepts_transfer_and_skips_counterpart(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    store.upsert_mapping(
        iban="CH222",
        ynab_account_id="savings-id",
        ynab_account_name="Savings",
        transfer_payee_id="tp-savings",
    )
    store.create_rule(
        name="Transfer category",
        enabled=True,
        operator="contains",
        pattern="transfer",
        replacement_payee=None,
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("250.00", "DBIT", "REF-D", "Transfer to savings"),
            opening="1000.00",
            closing="750.00",
        )
        + statement_xml(
            "CH222",
            entry_xml("250.00", "CRDT", "REF-C", "Transfer from checking"),
            opening="500.00",
            closing="750.00",
        )
    )

    upload = client.post(
        "/uploads",
        files={"file": ("multi.xml", content, "application/xml")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    transfer_id = job["payload"]["transfers"][0]["id"]

    response = client.post(
        f"/imports/{job_id}",
        data={"accepted_transfers": transfer_id},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(gateway.created) == 1
    assert gateway.created[0].account_id == "checking-id"
    assert gateway.created[0].transfer_payee_id == "tp-savings"
    assert gateway.created[0].category_id is None
    assert gateway.created[0].amount_milliunits == -250000
    updated_job = store.get_job(job_id)
    assert updated_job is not None
    assert updated_job["result"]["skipped_transfer_counterparts"] == ["INAB:REF-C"]


def test_import_page_shows_submitted_and_returned_ynab_transactions(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    gateway.returned_transaction_ids = ["ynab-1", "ynab-2"]
    gateway.returned_transactions = [
        {
            "id": "ynab-1",
            "date": "2026-04-10",
            "amount": "-10.000",
            "account_name": "Checking",
            "payee_name": "Payee",
            "import_id": "INAB:REF1",
            "matched_transaction_id": "manual-1",
        },
        {
            "id": "ynab-2",
            "date": "2026-04-10",
            "amount": "-10.000",
            "account_name": "Checking",
            "payee_name": "Payee",
            "import_id": "INAB:REF1",
        },
    ]
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Payee"),
            opening="100.00",
            closing="90.00",
        )
    )

    upload = client.post(
        "/uploads",
        files={"file": ("test.xml", content, "application/xml")},
        follow_redirects=False,
    )
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    imported = client.post(f"/imports/{job_id}", follow_redirects=True)

    assert imported.status_code == 200
    assert (
        "Submitted 1 bank row to YNAB. YNAB returned 2 saved transaction IDs."
        in imported.text
    )
    assert "Import result" in imported.text
    assert "Matched existing" in imported.text
    assert "Bank rows submitted by INAB" in imported.text
    assert "Transactions returned by YNAB" in imported.text
    assert "Matched existing transaction: manual-1" in imported.text
    assert "INAB:REF1" in imported.text
    job = store.get_job(job_id)
    assert job is not None
    assert job["result"]["submitted_transactions"][0]["import_id"] == "INAB:REF1"
    assert job["result"]["ynab_transactions"][0]["matched_transaction_id"] == "manual-1"
    assert job["result"]["ynab_matched_count"] == 1


def test_empty_statement_iban_does_not_block_preview(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Achat TWINT SBB MOBILE"),
            opening="100.00",
            closing="90.00",
        )
        + statement_xml("CH222", "", opening="500.00", closing="500.00")
    )

    upload = client.post(
        "/uploads",
        files={"file": ("multi.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    assert job["status"] == "preview"
    assert job["payload"]["missing_ibans"] == []


def test_preview_shows_summary_reconciliation_and_warnings(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("25.00", "DBIT", "REF1", "Payee"),
            opening="100.00",
            closing="80.00",
        )
    )

    response = client.post(
        "/uploads",
        files={"file": ("mismatch.xml", content, "application/xml")},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Import summary" in response.text
    assert "Ready rows" in response.text
    assert "Outflow" in response.text
    assert "Expected closing" in response.text
    assert "Balance mismatch for Checking" in response.text
    assert "75.00" in response.text
    assert "80.00" in response.text


def test_legacy_import_preview_without_reconciliation_payload_renders(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    job_id = store.create_job(
        filename="legacy.xml",
        status="preview",
        plan_id="plan-1",
        payload={
            "errors": [],
            "missing_ibans": [],
            "ignored_ibans": [],
            "statements": [
                {
                    "iban": "CH111",
                    "currency": "CHF",
                    "ynab_account_name": "Checking",
                    "transactions": [],
                    "movement_total": "0",
                    "balances_reconcile": True,
                }
            ],
            "rows": [
                {
                    "transaction": {
                        "booking_date": "2026-04-10",
                        "iban": "CH111",
                        "payee": "Payee",
                        "amount": "-10.00",
                        "import_id": "INAB:REF1",
                    },
                    "ynab_account_name": "Checking",
                    "status": "ready",
                }
            ],
            "transfers": [],
        },
    )

    response = client.get(f"/imports/{job_id}")

    assert response.status_code == 200
    assert "legacy.xml" in response.text
    assert "reconciled" in response.text
    assert "INAB:REF1" in response.text


def test_preview_warns_for_zero_ready_rows(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    gateway.existing[("plan-1", "checking-id")] = {"INAB:REF1"}
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Payee"),
            opening="100.00",
            closing="90.00",
        )
    )

    response = client.post(
        "/uploads",
        files={"file": ("duplicates.xml", content, "application/xml")},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "This import has zero ready rows." in response.text


def test_missing_iban_can_be_ignored_for_one_import(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Mapped"),
            opening="100.00",
            closing="90.00",
        )
        + statement_xml(
            "CH222",
            entry_xml("20.00", "DBIT", "REF2", "Unmapped"),
            opening="100.00",
            closing="80.00",
        )
    )

    upload = client.post(
        "/uploads",
        files={"file": ("multi.xml", content, "application/xml")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    assert job["status"] == "blocked"
    assert job["payload"]["missing_ibans"] == ["CH222"]

    ignored = client.post(
        f"/imports/{job_id}/ignored-ibans",
        data={"ignored_ibans": "CH222"},
        follow_redirects=False,
    )

    assert ignored.status_code == 303
    updated_job = store.get_job(job_id)
    assert updated_job is not None
    assert updated_job["status"] == "preview"
    assert updated_job["payload"]["missing_ibans"] == []
    assert updated_job["payload"]["ignored_ibans"] == ["CH222"]
    assert updated_job["payload"]["ignored_transaction_count"] == 1
    assert [row["transaction"]["iban"] for row in updated_job["payload"]["rows"]] == [
        "CH111"
    ]

    response = client.post(f"/imports/{job_id}", follow_redirects=False)

    assert response.status_code == 303
    assert len(gateway.created) == 1
    assert gateway.created[0].import_id == "INAB:REF1"


def test_upload_preview_marks_duplicates_even_when_other_ibans_are_unmapped(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    gateway.existing[("plan-1", "checking-id")] = {"INAB:REF1"}
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Mapped duplicate"),
            opening="100.00",
            closing="90.00",
        )
        + statement_xml(
            "CH222",
            entry_xml("20.00", "DBIT", "REF2", "Unmapped"),
            opening="100.00",
            closing="80.00",
        )
    )

    upload = client.post(
        "/uploads",
        files={"file": ("mixed.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    assert job["status"] == "blocked"
    assert job["payload"]["missing_ibans"] == ["CH222"]
    rows_by_iban = {row["transaction"]["iban"]: row for row in job["payload"]["rows"]}
    assert rows_by_iban["CH111"]["status"] == "duplicate"
    assert rows_by_iban["CH111"]["duplicate"] is True
    assert rows_by_iban["CH222"]["status"] == "missing_mapping"
    assert gateway.existing_calls == [("plan-1", "checking-id", date(2026, 4, 10))]


def test_upload_preview_marks_legacy_split_import_id_as_duplicate(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    uetr = "7f590b14-505f-4e16-8701-3fa01ee7a5a1"
    gateway.existing[("plan-1", "checking-id")] = {
        make_import_id(
            iban="CH111",
            source_ref=uetr,
            booking_date=date(2026, 4, 30),
            amount=Decimal("-600"),
            payee="Alex Example",
            memo=None,
        )
    }
    entry = f"""
<Ntry>
  <Amt Ccy="CHF">601.00</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-04-30</Dt></BookgDt>
  <ValDt><Dt>2026-04-30</Dt></ValDt>
  <AcctSvcrRef>ENTRYREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">1.00</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Other Example SA</Nm></Pty></Cdtr></RltdPties>
    </TxDtls>
    <TxDtls>
      <Amt Ccy="CHF">600.00</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <Refs><UETR>{uetr}</UETR></Refs>
      <RltdPties><Cdtr><Pty><Nm>Alex Example</Nm></Pty></Cdtr><CdtrAcct><Id><IBAN>CH0000000000000000005</IBAN></Id></CdtrAcct></RltdPties>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Ordre permanent</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="1000.00", closing="399.00")
    )

    upload = client.post(
        "/uploads",
        files={"file": ("legacy.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    rows_by_import_id = {
        row["transaction"]["import_id"]: row for row in job["payload"]["rows"]
    }
    assert rows_by_import_id["INAB:ENTRYREF.2"]["status"] == "duplicate"
    assert (
        rows_by_import_id["INAB:ENTRYREF.2"]["duplicate_match"]["match_type"]
        == "legacy_import_id"
    )


def test_upload_preview_matches_structured_reference_alias_only_with_same_date_and_amount(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    gateway.existing_transactions_by_account[("plan-1", "checking-id")] = [
        ExistingTransaction(
            import_id="INAB:DETAILREFONE",
            date=date(2026, 5, 8),
            amount=-19550,
        ),
        ExistingTransaction(
            import_id="INAB:DETAILREFTWO",
            date=date(2026, 5, 7),
            amount=-19550,
        ),
    ]
    entry = """
<Ntry>
  <Amt Ccy="CHF">39.10</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-05-08</Dt></BookgDt>
  <ValDt><Dt>2026-05-08</Dt></ValDt>
  <AcctSvcrRef>ENTRYREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">19.55</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Insurance Example SA</Nm></Pty></Cdtr></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>DETAILREFONE</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
    <TxDtls>
      <Amt Ccy="CHF">19.55</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Insurance Example SA</Nm></Pty></Cdtr></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>DETAILREFTWO</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Paiement groupé</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="100.00", closing="60.90")
    )

    upload = client.post(
        "/uploads",
        files={"file": ("structured.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    rows_by_import_id = {
        row["transaction"]["import_id"]: row for row in job["payload"]["rows"]
    }
    assert rows_by_import_id["INAB:ENTRYREF.1"]["status"] == "duplicate"
    assert rows_by_import_id["INAB:ENTRYREF.2"]["status"] == "ready"


def test_upload_preview_matches_structured_reference_alias_with_partial_existing_fingerprint(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    gateway.existing_transactions_by_account[("plan-1", "checking-id")] = [
        ExistingTransaction(
            import_id="INAB:DETAILREFONE",
            date=None,
            amount=-19550,
        ),
        ExistingTransaction(
            import_id="INAB:DETAILREFTWO",
            date=date(2026, 5, 8),
            amount=None,
        ),
    ]
    entry = """
<Ntry>
  <Amt Ccy="CHF">39.10</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-05-08</Dt></BookgDt>
  <ValDt><Dt>2026-05-08</Dt></ValDt>
  <AcctSvcrRef>ENTRYREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">19.55</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Insurance Example SA</Nm></Pty></Cdtr></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>DETAILREFONE</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
    <TxDtls>
      <Amt Ccy="CHF">19.55</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Insurance Example SA</Nm></Pty></Cdtr></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>DETAILREFTWO</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Paiement groupé</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="100.00", closing="60.90")
    )

    upload = client.post(
        "/uploads",
        files={"file": ("structured.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    assert {row["status"] for row in job["payload"]["rows"]} == {"duplicate"}


def test_upload_preview_does_not_match_ambiguous_structured_reference_alias_with_partial_fingerprint(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    gateway.existing_transactions_by_account[("plan-1", "checking-id")] = [
        ExistingTransaction(
            import_id="INAB:RECURRINGREF",
            date=None,
            amount=-26950,
        ),
    ]
    entry = """
<Ntry>
  <Amt Ccy="CHF">53.90</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-05-08</Dt></BookgDt>
  <ValDt><Dt>2026-05-08</Dt></ValDt>
  <AcctSvcrRef>ENTRYREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">26.95</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Telecom Example SA</Nm></Pty></Cdtr></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>RECURRINGREF</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
    <TxDtls>
      <Amt Ccy="CHF">26.95</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Telecom Example SA</Nm></Pty></Cdtr></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>RECURRINGREF</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Paiement groupé</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="100.00", closing="46.10")
    )

    upload = client.post(
        "/uploads",
        files={"file": ("structured.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    assert {row["status"] for row in job["payload"]["rows"]} == {"ready"}


def test_upload_preview_applies_rules_even_when_other_ibans_are_unmapped(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    store.create_rule(
        name="Mapped cleanup",
        enabled=True,
        operator="contains",
        pattern="mapped",
        replacement_payee="Mapped Payee",
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Mapped raw"),
            opening="100.00",
            closing="90.00",
        )
        + statement_xml(
            "CH222",
            entry_xml("20.00", "DBIT", "REF2", "Unmapped"),
            opening="100.00",
            closing="80.00",
        )
    )

    upload = client.post(
        "/uploads",
        files={"file": ("mixed.xml", content, "application/xml")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job = store.get_job(upload.headers["location"].rsplit("/", 1)[1])
    assert job is not None
    assert job["status"] == "blocked"
    rows_by_iban = {row["transaction"]["iban"]: row for row in job["payload"]["rows"]}
    mapped = rows_by_iban["CH111"]["transaction"]
    assert mapped["payee"] == "Mapped Payee"
    assert mapped["original_payee"] == "Mapped raw"
    assert mapped["category_id"] == "cat-food"
    assert mapped["applied_rule_name"] == "Mapped cleanup"


def test_setup_hides_mapped_and_dismissed_account_suggestions(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.observe_account(
        iban="CH111", currency="CHF", owner_name="Owner", bank_name="Bank"
    )
    store.observe_account(
        iban="CH222", currency="CHF", owner_name="Owner", bank_name="Bank"
    )
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )

    page = client.get("/setup")

    assert page.status_code == 200
    assert "No unmapped account suggestions" not in page.text
    suggestions = page.text.split("Mapping suggestions", 1)[1].split(
        "Manual mapping", 1
    )[0]
    assert "CH111" not in suggestions
    assert "CH222" in suggestions

    response = client.post(
        "/setup",
        data={"action": "dismiss_observed_account", "iban": "CH222"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "No unmapped account suggestions" in response.text
    assert "CH222" not in response.text


def test_csv_upload_requires_upload_account_selection(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    content = b""""Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"
"2026-04-30";"600.00";"";"";"";"Alex Example";"";"income";"";"no";"no"
"""

    upload = client.post(
        "/uploads",
        files={"file": ("other-bank.csv", content, "text/csv")},
        follow_redirects=True,
    )

    assert upload.status_code == 200
    assert "Select a YNAB account for this CSV upload" in upload.text


def test_csv_upload_uses_selected_upload_account(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    content = b""""Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"
"2026-04-30";"600.00";"";"";"";"Alex Example";"";"income";"";"no";"no"
"""

    upload = client.post(
        "/uploads",
        data={"csv_ynab_account_id": "savings-id"},
        files={"file": ("other-bank.csv", content, "text/csv")},
        follow_redirects=False,
    )

    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    assert job["status"] == "preview"
    assert job["payload"]["statements"][0]["ynab_account_name"] == "Savings"
    assert job["payload"]["rows"][0]["ynab_account_name"] == "Savings"
    assert job["payload"]["rows"][0]["transaction"]["payee"] == "Alex Example"
    assert job["payload"]["rows"][0]["transaction"]["iban"] == "CSV:SAVINGS-ID"

    response = client.post(f"/imports/{job_id}", follow_redirects=False)

    assert response.status_code == 303
    assert gateway.created[0].account_id == "savings-id"


def test_setup_hides_labeled_and_dismissed_counterparty_suggestions(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.observe_counterparty_account(
        iban="CH333", name="External One", bank_name="Bank"
    )
    store.observe_counterparty_account(
        iban="CH444", name="External Two", bank_name="Bank"
    )
    store.upsert_counterparty_mapping(iban="CH333", label="Pension")

    page = client.get("/setup")

    assert page.status_code == 200
    suggestions = page.text.split("Counterparty suggestions", 1)[1].split(
        "Saved counterparty labels", 1
    )[0]
    assert "CH333" not in suggestions
    assert "CH444" in suggestions

    response = client.post(
        "/setup",
        data={"action": "dismiss_observed_counterparty", "iban": "CH444"},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "No unlabeled counterparty suggestions" in response.text
    assert "CH444" not in response.text


def test_setup_saves_self_names_from_repeated_rows(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)

    response = client.post(
        "/setup",
        content="action=self_names&self_names=Alex+Example&self_names=&self_names=Example+Alex%2C+A+Example",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert store.self_names() == ["Alex Example", "Example Alex", "A Example"]


def test_rule_edited_csv_row_keeps_original_import_id(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    content = b""""Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"
"2026-04-30";"600.00";"";"";"";"Alex Example";"";"income";"";"no";"no"
"""
    first = client.post(
        "/uploads",
        data={"csv_ynab_account_id": "savings-id"},
        files={"file": ("other-bank.csv", content, "text/csv")},
        follow_redirects=False,
    )
    first_job = store.get_job(first.headers["location"].rsplit("/", 1)[1])
    assert first_job is not None
    original_import_id = first_job["payload"]["rows"][0]["transaction"]["import_id"]
    store.create_rule(
        name="Self",
        enabled=True,
        operator="contains",
        pattern="Alex",
        replacement_payee="Transfer from Alex",
        category_id="cat-food",
        category_name="Everyday: Food",
    )

    second = client.post(
        "/uploads",
        data={"csv_ynab_account_id": "savings-id"},
        files={"file": ("other-bank.csv", content, "text/csv")},
        follow_redirects=False,
    )
    second_job = store.get_job(second.headers["location"].rsplit("/", 1)[1])

    assert second_job is not None
    row = second_job["payload"]["rows"][0]["transaction"]
    assert row["payee"] == "Transfer from Alex"
    assert row["import_id"] == original_import_id


def test_self_named_counterparty_iban_mapping_relabels_transfer(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.save_self_names(["Alex Example"])
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    store.upsert_counterparty_mapping(
        iban="CH0000000000000000005", label="External savings"
    )
    entry = """
<Ntry>
  <Amt Ccy="CHF">600.00</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-04-30</Dt></BookgDt>
  <ValDt><Dt>2026-04-30</Dt></ValDt>
  <AcctSvcrRef>SELFREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">600.00</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties>
        <Cdtr><Pty><Nm>Alex Example</Nm></Pty></Cdtr>
        <CdtrAcct><Id><IBAN>CH0000000000000000005</IBAN></Id></CdtrAcct>
      </RltdPties>
      <RltdAgts><CdtrAgt><FinInstnId><Nm>Example Bank AG</Nm></FinInstnId></CdtrAgt></RltdAgts>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Ordre permanent</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="1000.00", closing="400.00")
    )

    upload = client.post(
        "/uploads",
        files={"file": ("self.xml", content, "application/xml")},
        follow_redirects=False,
    )
    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    row = job["payload"]["rows"][0]
    assert row["transaction"]["payee"] == "Transfer to External savings"
    assert "Self-transfer account: External savings" in row["transaction"]["memo"]
    assert (
        store.list_observed_counterparty_accounts()[0]["iban"]
        == "CH0000000000000000005"
    )

    response = client.post(f"/imports/{job_id}", follow_redirects=False)

    assert response.status_code == 303
    assert gateway.created[0].payee_name == "Transfer to External savings"


def test_import_history_lists_jobs(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    first = store.create_job(
        filename="preview.xml",
        status="preview",
        plan_id="plan-1",
        payload={
            "transaction_count": 2,
            "ready_count": 1,
            "duplicate_count": 1,
            "transfers": [],
        },
    )
    imported = store.create_job(
        filename="imported.xml",
        status="imported",
        plan_id="plan-1",
        payload={
            "transaction_count": 3,
            "ready_count": 3,
            "duplicate_count": 0,
            "transfers": [{"id": "t1"}],
        },
    )
    store.update_job(
        imported,
        status="imported",
        result={"created_count": 2, "transaction_ids": ["ynab-1", "ynab-2"]},
    )
    assert first

    page = client.get("/imports")

    assert page.status_code == 200
    assert "preview.xml" in page.text
    assert "imported.xml" in page.text
    assert "imported" in page.text
    assert "3</strong> ready" in page.text
    assert "2</strong> saved IDs" in page.text


def test_imported_job_can_be_undone(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Payee"),
            opening="100.00",
            closing="90.00",
        )
    )
    upload = client.post(
        "/uploads",
        files={"file": ("test.xml", content, "application/xml")},
        follow_redirects=False,
    )
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    imported = client.post(f"/imports/{job_id}", follow_redirects=False)
    assert imported.status_code == 303

    undo = client.post(f"/imports/{job_id}/undo", follow_redirects=False)

    assert undo.status_code == 303
    assert gateway.deleted == [("plan-1", "ynab-1")]
    job = store.get_job(job_id)
    assert job is not None
    assert job["status"] == "reverted"
    assert job["result"]["undo"]["deleted_transaction_ids"] == ["ynab-1"]
    assert job["result"]["undo"]["errors"] == []


def test_undo_is_not_exposed_for_preview_jobs(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(
        iban="CH111",
        ynab_account_id="checking-id",
        ynab_account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Payee"),
            opening="100.00",
            closing="90.00",
        )
    )

    page = client.post(
        "/uploads",
        files={"file": ("test.xml", content, "application/xml")},
        follow_redirects=True,
    )

    assert page.status_code == 200
    assert "Undo import" not in page.text


def test_partial_undo_failure_keeps_error_details(
    app_client: tuple[TestClient, Store, FakeGateway],
) -> None:
    client, store, gateway = app_client
    login(client)
    job_id = store.create_job(
        filename="imported.xml", status="imported", plan_id="plan-1", payload={}
    )
    store.update_job(
        job_id,
        status="imported",
        result={"created_count": 2, "transaction_ids": ["ynab-1", "ynab-2"]},
    )
    gateway.delete_errors["ynab-2"] = "boom"

    response = client.post(f"/imports/{job_id}/undo", follow_redirects=False)

    assert response.status_code == 303
    assert gateway.deleted == [("plan-1", "ynab-1")]
    job = store.get_job(job_id)
    assert job is not None
    assert job["status"] == "imported"
    assert job["result"]["undo"]["deleted_transaction_ids"] == ["ynab-1"]
    assert job["result"]["undo"]["errors"] == ["ynab-2: boom"]


def test_save_plan_accepts_uuid_like_ids(tmp_path) -> None:
    store = Store(tmp_path / "inab.sqlite3")
    gateway = FakeGateway()
    gateway.plans = [YnabPlan(id=UUID("12345678-1234-5678-1234-567812345678"), name="Household")]  # type: ignore[arg-type]

    _save_plan(store, gateway, "12345678-1234-5678-1234-567812345678")

    assert store.selected_plan() == (
        "12345678-1234-5678-1234-567812345678",
        "Household",
    )


def test_format_money_uses_two_decimals_and_swiss_grouping() -> None:
    assert format_money("-1.5") == "-1.50"
    assert format_money("-1500") == "-1'500.00"
    assert format_money("8984") == "8'984.00"
