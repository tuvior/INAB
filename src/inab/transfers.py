from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .models import BankTransaction


@dataclass(frozen=True)
class TransferPair:
    id: str
    debit_import_id: str
    credit_import_id: str
    source_iban: str
    target_iban: str
    amount: str
    currency: str
    date_distance_days: int
    reason: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "id": self.id,
            "debit_import_id": self.debit_import_id,
            "credit_import_id": self.credit_import_id,
            "source_iban": self.source_iban,
            "target_iban": self.target_iban,
            "amount": self.amount,
            "currency": self.currency,
            "date_distance_days": self.date_distance_days,
            "reason": self.reason,
        }


def detect_transfer_pairs(
    transactions: list[BankTransaction], *, max_days: int = 3
) -> list[TransferPair]:
    debits = [tx for tx in transactions if tx.amount < 0]
    credits = [tx for tx in transactions if tx.amount > 0]
    debit_candidates: dict[str, list[tuple[BankTransaction, int]]] = {}
    credit_candidates: dict[str, list[BankTransaction]] = {}

    for debit in debits:
        matches: list[tuple[BankTransaction, int]] = []
        for credit in credits:
            if debit.iban == credit.iban:
                continue
            if debit.currency != credit.currency:
                continue
            if abs(debit.amount) != credit.amount:
                continue
            distance = _date_distance(debit, credit)
            if distance <= max_days:
                matches.append((credit, distance))
                credit_candidates.setdefault(credit.import_id, []).append(debit)
        debit_candidates[debit.import_id] = matches

    pairs: list[TransferPair] = []
    used_debits: set[str] = set()
    used_credits: set[str] = set()
    for debit in debits:
        matches = debit_candidates.get(debit.import_id, [])
        if len(matches) != 1:
            continue
        credit, distance = matches[0]
        if len(credit_candidates.get(credit.import_id, [])) != 1:
            continue
        if debit.import_id in used_debits or credit.import_id in used_credits:
            continue
        used_debits.add(debit.import_id)
        used_credits.add(credit.import_id)
        pair_id = hashlib.sha1(
            f"{debit.import_id}|{credit.import_id}".encode("utf-8")
        ).hexdigest()[:12]
        pairs.append(
            TransferPair(
                id=f"tr_{pair_id}",
                debit_import_id=debit.import_id,
                credit_import_id=credit.import_id,
                source_iban=debit.iban,
                target_iban=credit.iban,
                amount=str(credit.amount),
                currency=credit.currency,
                date_distance_days=distance,
                reason="Same amount, opposite signs, mapped accounts, and dates within 3 days.",
            )
        )
    return pairs


def _date_distance(left: BankTransaction, right: BankTransaction) -> int:
    left_dates = [left.booking_date]
    right_dates = [right.booking_date]
    if left.value_date:
        left_dates.append(left.value_date)
    if right.value_date:
        right_dates.append(right.value_date)
    return min(
        abs((left_date - right_date).days)
        for left_date in left_dates
        for right_date in right_dates
    )
