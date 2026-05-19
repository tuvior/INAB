from __future__ import annotations

from inab.transfers import detect_transfer_pairs

from conftest import tx


def test_detects_unambiguous_internal_transfer() -> None:
    pairs = detect_transfer_pairs(
        [
            tx("debit", "CH111", "-250.00", booking_date="2026-04-10", value_date="2026-04-10"),
            tx("credit", "CH222", "250.00", booking_date="2026-04-12", value_date="2026-04-12"),
        ]
    )

    assert len(pairs) == 1
    assert pairs[0].source_iban == "CH111"
    assert pairs[0].target_iban == "CH222"
    assert pairs[0].date_distance_days == 2


def test_ambiguous_transfer_is_not_preselected() -> None:
    pairs = detect_transfer_pairs(
        [
            tx("debit", "CH111", "-100.00"),
            tx("credit-a", "CH222", "100.00"),
            tx("credit-b", "CH333", "100.00"),
        ]
    )

    assert pairs == []
