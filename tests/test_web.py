from __future__ import annotations

from uuid import UUID

from fastapi.testclient import TestClient

from inab.config import Settings
from inab.store import Store
from inab.web import _save_plan, create_app, format_money
from inab.ynab_api import YnabPlan

from conftest import FakeGateway, camt_document, entry_xml, login, statement_xml


def test_auth_gate_redirects_to_login(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, _, _ = app_client

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_rules_page_requires_auth(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, _, _ = app_client

    response = client.get("/rules", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_root_path_uses_relative_prefixed_template_urls(tmp_path, fake_gateway: FakeGateway) -> None:
    settings = Settings(
        data_dir=tmp_path,
        ynab_access_token="fake-token",
        username="inab",
        password="secret",
        session_secret="test-session",
        root_path="/inab",
    )
    app = create_app(settings=settings, store=Store(settings.database_path), gateway_factory=lambda _settings: fake_gateway)
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


def test_root_path_redirects_to_prefixed_paths(tmp_path, fake_gateway: FakeGateway) -> None:
    settings = Settings(
        data_dir=tmp_path,
        ynab_access_token="fake-token",
        username="inab",
        password="secret",
        session_secret="test-session",
        root_path="/inab",
    )
    app = create_app(settings=settings, store=Store(settings.database_path), gateway_factory=lambda _settings: fake_gateway)
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


def test_rules_page_loads_visible_categories_and_saves_rule(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
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


def test_rules_page_lists_payees_and_rule_effects(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
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


def test_rules_page_tests_fake_transaction_first_match_wins(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
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


def test_rules_page_displays_invalid_rule_errors(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
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


def test_upload_preview_applies_rule_metadata_and_import_sends_category(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(iban="CH111", ynab_account_id="checking-id", ynab_account_name="Checking", transfer_payee_id="tp-checking")
    store.create_rule(
        name="SBB travel",
        enabled=True,
        operator="contains",
        pattern="sbb",
        replacement_payee="SBB",
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    content = camt_document(statement_xml("CH111", entry_xml("10.00", "DBIT", "REF1", "Achat TWINT SBB MOBILE"), opening="100.00", closing="90.00"))

    upload = client.post("/uploads", files={"file": ("test.xml", content, "application/xml")}, follow_redirects=False)
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
    assert gateway.created[0]["payee_name"] == "SBB"
    assert gateway.created[0]["category_id"] == "cat-food"


def test_import_accepts_transfer_and_skips_counterpart(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.upsert_mapping(iban="CH111", ynab_account_id="checking-id", ynab_account_name="Checking", transfer_payee_id="tp-checking")
    store.upsert_mapping(iban="CH222", ynab_account_id="savings-id", ynab_account_name="Savings", transfer_payee_id="tp-savings")
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
    assert "category_id" not in gateway.created[0]
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


def test_setup_hides_mapped_and_dismissed_account_suggestions(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.observe_account(iban="CH111", currency="CHF", owner_name="Owner", bank_name="Bank")
    store.observe_account(iban="CH222", currency="CHF", owner_name="Owner", bank_name="Bank")
    store.upsert_mapping(iban="CH111", ynab_account_id="checking-id", ynab_account_name="Checking", transfer_payee_id="tp-checking")

    page = client.get("/setup")

    assert page.status_code == 200
    assert "No unmapped account suggestions" not in page.text
    suggestions = page.text.split("Mapping suggestions", 1)[1].split("Manual mapping", 1)[0]
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


def test_csv_upload_requires_upload_account_selection(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    content = b'''"Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"
"2026-04-30";"600.00";"";"";"";"Alex Example";"";"income";"";"no";"no"
'''

    upload = client.post("/uploads", files={"file": ("other-bank.csv", content, "text/csv")}, follow_redirects=True)

    assert upload.status_code == 200
    assert "Select a YNAB account for this CSV upload" in upload.text


def test_csv_upload_uses_selected_upload_account(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    content = b'''"Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"
"2026-04-30";"600.00";"";"";"";"Alex Example";"";"income";"";"no";"no"
'''

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
    assert gateway.created[0]["account_id"] == "savings-id"


def test_setup_hides_labeled_and_dismissed_counterparty_suggestions(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.observe_counterparty_account(iban="CH333", name="External One", bank_name="Bank")
    store.observe_counterparty_account(iban="CH444", name="External Two", bank_name="Bank")
    store.upsert_counterparty_mapping(iban="CH333", label="Pension")

    page = client.get("/setup")

    assert page.status_code == 200
    suggestions = page.text.split("Counterparty suggestions", 1)[1].split("Saved counterparty labels", 1)[0]
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


def test_rule_edited_csv_row_keeps_original_import_id(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, _ = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    content = b'''"Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"
"2026-04-30";"600.00";"";"";"";"Alex Example";"";"income";"";"no";"no"
'''
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


def test_self_named_counterparty_iban_mapping_relabels_transfer(app_client: tuple[TestClient, Store, FakeGateway]) -> None:
    client, store, gateway = app_client
    login(client)
    store.save_selected_plan("plan-1", "Household")
    store.save_self_names(["Alex Example"])
    store.upsert_mapping(iban="CH111", ynab_account_id="checking-id", ynab_account_name="Checking", transfer_payee_id="tp-checking")
    store.upsert_counterparty_mapping(iban="CH0000000000000000005", label="External savings")
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
    content = camt_document(statement_xml("CH111", entry, opening="1000.00", closing="400.00"))

    upload = client.post("/uploads", files={"file": ("self.xml", content, "application/xml")}, follow_redirects=False)
    assert upload.status_code == 303
    job_id = upload.headers["location"].rsplit("/", 1)[1]
    job = store.get_job(job_id)
    assert job is not None
    row = job["payload"]["rows"][0]
    assert row["transaction"]["payee"] == "Transfer to External savings"
    assert "Self-transfer account: External savings" in row["transaction"]["memo"]
    assert store.list_observed_counterparty_accounts()[0]["iban"] == "CH0000000000000000005"

    response = client.post(f"/imports/{job_id}", follow_redirects=False)

    assert response.status_code == 303
    assert gateway.created[0]["payee_name"] == "Transfer to External savings"


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
