from __future__ import annotations

from datetime import date
from decimal import Decimal

from inab.budget_api import ImportTransaction
from inab.ynab_api import _to_ynab_payload


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
