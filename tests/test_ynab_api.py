from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from inab.budget_api import ImportTransaction
from inab.ynab_api import OfficialYnabGateway, _to_ynab_payload


def test_ynab_payload_for_normal_transaction_preserves_existing_fields() -> None:
    payload = _to_ynab_payload(
        ImportTransaction(
            account_id="checking-id",
            date=date(2026, 4, 10),
            amount=Decimal("-10.00"),
            amount_milliunits=-10000,
            payee_name="SBB",
            memo="Memo",
            import_id="INAB:REF1",
            category_id="cat-food",
        )
    )

    assert payload == {
        "account_id": "checking-id",
        "date": "2026-04-10",
        "amount": -10000,
        "memo": "Memo",
        "cleared": "cleared",
        "approved": False,
        "import_id": "INAB:REF1",
        "payee_name": "SBB",
        "category_id": "cat-food",
    }


def test_ynab_payload_for_transfer_uses_transfer_payee_and_no_category() -> None:
    payload = _to_ynab_payload(
        ImportTransaction(
            account_id="checking-id",
            date=date(2026, 4, 10),
            amount=Decimal("-250.00"),
            amount_milliunits=-250000,
            payee_name="Transfer to Savings",
            memo=None,
            import_id="INAB:DEBIT",
            category_id="cat-food",
            transfer_payee_id="tp-savings",
        )
    )

    assert payload == {
        "account_id": "checking-id",
        "date": "2026-04-10",
        "amount": -250000,
        "cleared": "cleared",
        "approved": False,
        "import_id": "INAB:DEBIT",
        "payee_id": "tp-savings",
    }


def test_ynab_gateway_fetches_transaction_flag_details(monkeypatch: Any) -> None:
    transactions = [
        SimpleNamespace(
            id="tx-1",
            flag_color=SimpleNamespace(value="green"),
            flag_name="Paid for others",
        ),
        SimpleNamespace(id="tx-2", flag_color="purple", flag_name="Shared"),
        SimpleNamespace(id="tx-3", flag_color=None, flag_name=None),
    ]

    class FakeApiClient:
        def __init__(self, configuration: Any) -> None:
            self.configuration = configuration

        def __enter__(self) -> "FakeApiClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

    class FakeTransactionsApi:
        def __init__(self, api_client: Any) -> None:
            self.api_client = api_client

        def get_transactions(self, budget_id: str) -> Any:
            assert budget_id == "budget-1"
            return SimpleNamespace(data=SimpleNamespace(transactions=transactions))

    fake_ynab = SimpleNamespace(
        ApiClient=FakeApiClient,
        Configuration=lambda access_token: SimpleNamespace(access_token=access_token),
        TransactionsApi=FakeTransactionsApi,
    )
    monkeypatch.setattr("inab.ynab_api._ynab", lambda: fake_ynab)

    details = OfficialYnabGateway("token").transaction_flag_details("budget-1")

    assert details == {
        "tx-1": {"flag_color": "green", "flag_name": "Paid for others"},
        "tx-2": {"flag_color": "purple", "flag_name": "Shared"},
    }
