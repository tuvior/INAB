from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inab.config import Settings
from inab.models import BankTransaction
from inab.store import Store
from inab.web import create_app
from inab.ynab_api import CreateTransactionsResult, YnabAccount, YnabPlan


def camt_document(statements: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>MSG</MsgId></GrpHdr>
    {statements}
  </BkToCstmrStmt>
</Document>
""".encode()


def statement_xml(iban: str, entries: str, *, opening: str = "1000.00", closing: str = "1000.00", currency: str = "CHF") -> str:
    return f"""
<Stmt>
  <Id>STM-{iban}</Id>
  <FrToDt>
    <FrDtTm>2026-04-01T00:00:00+02:00</FrDtTm>
    <ToDtTm>2026-04-30T23:59:59+02:00</ToDtTm>
  </FrToDt>
  <Acct>
    <Id><IBAN>{iban}</IBAN></Id>
    <Ccy>{currency}</Ccy>
    <Ownr><Nm>Owner {iban}</Nm></Ownr>
    <Svcr><FinInstnId><Nm>Test Bank</Nm></FinInstnId></Svcr>
  </Acct>
  <Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="{currency}">{opening}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-04-01</Dt></Dt></Bal>
  <Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="{currency}">{closing}</Amt><CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>2026-04-30</Dt></Dt></Bal>
  {entries}
</Stmt>
"""


def entry_xml(
    amount: str,
    indicator: str,
    ref: str,
    text: str,
    *,
    booking_date: str = "2026-04-10",
    value_date: str = "2026-04-10",
    currency: str = "CHF",
) -> str:
    return f"""
<Ntry>
  <Amt Ccy="{currency}">{amount}</Amt>
  <CdtDbtInd>{indicator}</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>{booking_date}</Dt></BookgDt>
  <ValDt><Dt>{value_date}</Dt></ValDt>
  <AcctSvcrRef>{ref}</AcctSvcrRef>
  <AddtlNtryInf>{text}</AddtlNtryInf>
</Ntry>
"""


def tx(
    import_id: str,
    iban: str,
    amount: str,
    *,
    booking_date: str = "2026-04-10",
    value_date: str | None = "2026-04-10",
) -> BankTransaction:
    return BankTransaction(
        uid=import_id,
        statement_id="stmt",
        iban=iban,
        currency="CHF",
        booking_date=date.fromisoformat(booking_date),
        value_date=date.fromisoformat(value_date) if value_date else None,
        amount=Decimal(amount),
        payee=f"Payee {import_id}",
        memo=f"Memo {import_id}",
        source_ref=import_id,
        import_id=import_id,
        sequence=1,
    )


class FakeGateway:
    def __init__(self) -> None:
        self.plans = [YnabPlan(id="plan-1", name="Household")]
        self.accounts = [
            YnabAccount(id="checking-id", name="Checking", type="checking", closed=False, deleted=False, transfer_payee_id="tp-checking"),
            YnabAccount(id="savings-id", name="Savings", type="savings", closed=False, deleted=False, transfer_payee_id="tp-savings"),
        ]
        self.existing: dict[tuple[str, str], set[str]] = {}
        self.created: list[dict[str, Any]] = []

    def list_plans(self) -> list[YnabPlan]:
        return self.plans

    def list_accounts(self, plan_id: str) -> list[YnabAccount]:
        return self.accounts

    def existing_import_ids(self, plan_id: str, account_id: str, since_date: date) -> set[str]:
        return self.existing.get((plan_id, account_id), set())

    def create_transactions(self, plan_id: str, transactions: list[dict[str, Any]]) -> CreateTransactionsResult:
        self.created.extend(transactions)
        return CreateTransactionsResult(transaction_ids=[f"ynab-{i}" for i, _ in enumerate(transactions, start=1)], duplicate_import_ids=[])


@pytest.fixture
def fake_gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def app_client(tmp_path: Path, fake_gateway: FakeGateway) -> tuple[TestClient, Store, FakeGateway]:
    settings = Settings(
        data_dir=tmp_path,
        ynab_access_token="fake-token",
        username="inab",
        password="secret",
        session_secret="test-session",
    )
    store = Store(settings.database_path)
    app = create_app(settings=settings, store=store, gateway_factory=lambda _settings: fake_gateway)
    return TestClient(app), store, fake_gateway


def login(client: TestClient) -> None:
    response = client.post("/login", data={"username": "inab", "password": "secret", "next": "/"}, follow_redirects=False)
    assert response.status_code == 303
