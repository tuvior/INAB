from __future__ import annotations

from datetime import date
from decimal import Decimal

from inab.models import (
    amount_to_milliunits,
    make_camt_missing_ref_import_id,
    make_csv_missing_ref_import_id,
    make_source_ref_import_id,
)


def test_amount_to_milliunits_uses_ynab_format() -> None:
    assert amount_to_milliunits(Decimal("-58.95")) == -58950
    assert amount_to_milliunits(Decimal("1.8")) == 1800


def test_import_id_uses_bank_reference_when_short_enough() -> None:
    import_id = make_source_ref_import_id(
        iban="CH111",
        source_ref="20368112128",
    )

    assert import_id == "INAB:20368112128"
    assert len(import_id) <= 36


def test_csv_import_id_uses_stable_source_identity_fields() -> None:
    kwargs = {
        "account_key": "CSV:SAVINGS-ID",
        "booking_date": date(2026, 4, 30),
        "amount": Decimal("600.00"),
        "identity_fields": ["Alex Example", "Salary April", "", "", ""],
        "occurrence": 1,
    }

    assert make_csv_missing_ref_import_id(**kwargs) == make_csv_missing_ref_import_id(
        **kwargs
    )
    assert len(make_csv_missing_ref_import_id(**kwargs)) <= 36
    assert make_csv_missing_ref_import_id(**kwargs) != make_csv_missing_ref_import_id(
        **{**kwargs, "occurrence": 2}
    )
    assert make_csv_missing_ref_import_id(**kwargs) != make_csv_missing_ref_import_id(
        **{
            **kwargs,
            "identity_fields": ["Alex Example", "Salary May", "", "", ""],
        }
    )


def test_camt_missing_ref_import_id_uses_stable_source_identity_fields() -> None:
    kwargs = {
        "iban": "CH111",
        "booking_date": date(2026, 4, 30),
        "amount": Decimal("-10.00"),
        "identity_fields": [
            "bank_booking_date=2026-04-30",
            "value_date=2026-04-30",
            "description=Raw bank text",
        ],
        "occurrence": 1,
    }

    assert make_camt_missing_ref_import_id(**kwargs) == make_camt_missing_ref_import_id(
        **kwargs
    )
    assert len(make_camt_missing_ref_import_id(**kwargs)) <= 36
    assert make_camt_missing_ref_import_id(**kwargs) != make_camt_missing_ref_import_id(
        **{**kwargs, "occurrence": 2}
    )
