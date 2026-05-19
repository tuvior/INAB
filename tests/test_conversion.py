from __future__ import annotations

from datetime import date
from decimal import Decimal

from inab.models import amount_to_milliunits, make_import_id


def test_amount_to_milliunits_uses_ynab_format() -> None:
    assert amount_to_milliunits(Decimal("-58.95")) == -58950
    assert amount_to_milliunits(Decimal("1.8")) == 1800


def test_import_id_uses_bank_reference_when_short_enough() -> None:
    import_id = make_import_id(
        iban="CH111",
        source_ref="20368112128",
        booking_date=date(2026, 4, 1),
        amount=Decimal("-58.95"),
        payee="Insurance Example AG",
        memo="Insurance Example AG",
    )

    assert import_id == "INAB:20368112128"
    assert len(import_id) <= 36


def test_fallback_import_id_is_stable_and_limited() -> None:
    kwargs = {
        "iban": "CH111",
        "source_ref": None,
        "booking_date": date(2026, 4, 1),
        "amount": Decimal("-10.00"),
        "payee": "Payee",
        "memo": "Memo",
        "occurrence": 1,
    }

    assert make_import_id(**kwargs) == make_import_id(**kwargs)
    assert len(make_import_id(**kwargs)) <= 36
    assert make_import_id(**kwargs) != make_import_id(**{**kwargs, "occurrence": 2})
