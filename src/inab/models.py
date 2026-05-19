from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


REF_MISSING_VALUES = {"", "NONREF", "NOTPROVIDED", "NOT PROVIDED", "N/A", "NA"}
GENERIC_PAYEES = {"paiement groupé", "paiement groupe", "ordre permanent"}
EXACT_PAYEE_ALIASES = {
    "ALIEXPRESS": "AliExpress",
    "BKG BOOKING.COM HOTEL": "Booking.com",
    "BOOKING.COM HOTEL": "Booking.com",
    "DIGITEC GALAXUS": "Digitec Galaxus",
    "JUSTEAT": "Just Eat",
    "PARKINGPAY": "ParkingPay",
    "PLAYSTATION": "PlayStation",
    "QOQA": "QoQa",
    "SBB EASYRIDE": "SBB EasyRide",
    "SBB MOBILE": "SBB Mobile",
    "SERVICE NAVIGO 4042878": "Service Navigo",
    "SPOTIFY": "Spotify",
    "SPOTIFYCH": "Spotify",
}
PATTERN_PAYEE_ALIASES = (
    (r"^COOP\b.*\bPARK", "Coop Parking"),
    (r"^MIGROS\b.*\bPARK", "Migros Parking"),
    (r"^COOP-\d+\b", "Coop"),
    (r"^COOP PRONTO \d+\b", "Coop Pronto"),
    (r"^MIGROS(?:\b|-)", "Migros"),
    (r"^JUMBO-\d+\b", "Jumbo"),
)
PREFIX_PAYEE_ALIASES = (
    ("SPOTIFY ", "Spotify"),
    ("SERVICE NAVIGO ", "Service Navigo"),
    ("BKG BOOKING.COM ", "Booking.com"),
    ("BOOKING.COM ", "Booking.com"),
    ("SBB CFF FFS MOBILE", "SBB Mobile"),
    ("IKEA SA ", "IKEA"),
    ("DECATHLON SPORTS ", "Decathlon"),
    ("DENNER DISCOUNT ", "Denner"),
    ("MCDONALDS ", "McDonald's"),
    ("TCS ", "TCS"),
)
UPPERCASE_WORDS = {"AG", "AI", "B.V.", "GMBH", "SA", "SBB", "V", "VISA"}
LOWERCASE_WORDS = {"de", "des", "du", "for", "la", "le", "of", "the"}


def normalize_whitespace(value: str | None) -> str:
    if not value:
        return ""
    lines = [" ".join(line.split()) for line in value.replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line).strip()


def compact_whitespace(value: str | None) -> str:
    return " ".join(normalize_whitespace(value).split())


def first_line(value: str, fallback: str = "Unknown payee") -> str:
    for line in normalize_whitespace(value).split("\n"):
        if line.strip():
            return line.strip()[:200]
    return fallback


def payee_from_description(value: str, fallback: str = "Unknown payee") -> str:
    raw = first_line(value, fallback=fallback)
    normalized = normalize_whitespace(raw)
    if not normalized or normalized == fallback:
        return fallback

    lowered = normalized.casefold()
    if lowered in GENERIC_PAYEES:
        return normalized[:200]

    merchant = _strip_payee_prefix(normalized)
    merchant = _strip_card_purchase_details(merchant)
    merchant = _strip_noise_tokens(merchant)
    merchant = _normalize_person_name(merchant)
    if not merchant:
        return normalized[:200]
    return _canonical_payee(merchant)[:200]


def _strip_payee_prefix(value: str) -> str:
    prefix_patterns = (
        r"^Achat\s+TWINT\s+",
        r"^Paiement\s+TWINT\s+",
        r"^Cr[ée]dit\s+TWINT\s+",
        r"^Achat\s+online\s+",
        r"^Achat\s+",
        r"^Paiement\s+",
        r"^Cr[ée]dit\s+",
    )
    for pattern in prefix_patterns:
        stripped = re.sub(pattern, "", value, flags=re.IGNORECASE)
        if stripped != value:
            return stripped
    if re.match(r"^Prix du paquet Raiffeisen MemberPlus\b", value, flags=re.IGNORECASE):
        return "Raiffeisen MemberPlus"
    if re.match(r"^Taxe carte de cr[ée]dit\b", value, flags=re.IGNORECASE):
        return "Taxe carte de crédit"
    return value


def _strip_card_purchase_details(value: str) -> str:
    value = re.sub(r"\s+\d{2}\.\d{2}\.\d{4},\s*\d{2}:\d{2},\s*No carte\b.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r",\s*No carte\b.*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+No carte\b.*$", "", value, flags=re.IGNORECASE)
    return value


def _strip_noise_tokens(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*\*+\s*", " ", value)
    value = re.sub(r"-TWINT$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+P[0-9A-Z]{6,}$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\bS\s+A\b", "SA", value, flags=re.IGNORECASE)
    value = re.sub(r"\bB\s*\.?\s*V\.?\b", "B.V.", value, flags=re.IGNORECASE)
    return value.strip(" -,.")


def _normalize_person_name(value: str) -> str:
    match = re.fullmatch(r"([A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ' -]+),\s*([A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ' -]+)", value)
    if not match:
        return value
    last_name, first_name = match.groups()
    return f"{first_name} {last_name}"


def _canonical_payee(value: str) -> str:
    collapsed = re.sub(r"\s+", " ", value).strip()
    lookup = collapsed.upper()
    alias = _payee_alias(lookup)
    if alias:
        return alias
    if _should_smart_title(collapsed):
        return _smart_title(collapsed)
    return collapsed


def _payee_alias(lookup: str) -> str | None:
    if lookup in EXACT_PAYEE_ALIASES:
        return EXACT_PAYEE_ALIASES[lookup]
    if lookup == "TOURING CLUB SUISSE (TCS)":
        return "TCS"
    for pattern, canonical in PATTERN_PAYEE_ALIASES:
        if re.match(pattern, lookup):
            return canonical
    for prefix, canonical in PREFIX_PAYEE_ALIASES:
        if lookup.startswith(prefix):
            return canonical
    return None


def _should_smart_title(value: str) -> bool:
    letters = [char for char in value if char.isalpha()]
    if not letters:
        return False
    uppercase = sum(1 for char in letters if char.isupper())
    lowercase = sum(1 for char in letters if char.islower())
    return uppercase > 0 and lowercase == 0


def _smart_title(value: str) -> str:
    tokens = re.split(r"([\s,/-]+)", value)
    rendered: list[str] = []
    word_index = 0
    for token in tokens:
        if not token or re.fullmatch(r"[\s,/-]+", token):
            rendered.append(token)
            continue
        upper = token.upper()
        lower = token.lower()
        if upper in EXACT_PAYEE_ALIASES:
            rendered.append(EXACT_PAYEE_ALIASES[upper])
        elif lower in LOWERCASE_WORDS and word_index > 0:
            rendered.append(lower)
        elif upper in UPPERCASE_WORDS or (len(token) <= 3 and token.isalpha() and token.isupper() and lower not in LOWERCASE_WORDS):
            rendered.append(upper)
        else:
            rendered.append(token.capitalize())
        word_index += 1
    return "".join(rendered).strip()


def truncate(value: str | None, limit: int) -> str | None:
    if not value:
        return None
    normalized = compact_whitespace(value)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "..."


def clean_source_ref(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip()
    if normalized.upper() in REF_MISSING_VALUES:
        return None
    return normalized


def make_import_id(
    *,
    iban: str,
    source_ref: str | None,
    booking_date: date,
    amount: Decimal,
    payee: str,
    memo: str | None,
    occurrence: int = 1,
) -> str:
    if source_ref:
        token = re.sub(r"[^A-Za-z0-9._-]", "", source_ref)
        if token and len(f"INAB:{token}") <= 36:
            return f"INAB:{token}"
        digest = hashlib.sha1(f"{iban}|{source_ref}".encode("utf-8")).hexdigest()[:30]
        return f"INAB:{digest}"

    fingerprint = "|".join(
        [
            iban,
            booking_date.isoformat(),
            str(amount),
            payee,
            memo or "",
            str(occurrence),
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:30]
    return f"INAB:{digest}"


def amount_to_milliunits(amount: Decimal) -> int:
    return int((amount * Decimal("1000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class Balance:
    kind: str
    amount: Decimal
    indicator: str
    balance_date: date | None
    currency: str

    @property
    def signed_amount(self) -> Decimal:
        return self.amount if self.indicator == "CRDT" else -self.amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "amount": str(self.amount),
            "indicator": self.indicator,
            "balance_date": self.balance_date.isoformat() if self.balance_date else None,
            "currency": self.currency,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Balance":
        return cls(
            kind=data["kind"],
            amount=Decimal(data["amount"]),
            indicator=data["indicator"],
            balance_date=date.fromisoformat(data["balance_date"]) if data.get("balance_date") else None,
            currency=data["currency"],
        )


@dataclass
class BankTransaction:
    uid: str
    statement_id: str
    iban: str
    currency: str
    booking_date: date
    value_date: date | None
    amount: Decimal
    payee: str
    memo: str | None
    source_ref: str | None
    import_id: str
    sequence: int
    bank_code: str | None = None
    counterparty_name: str | None = None
    counterparty_iban: str | None = None
    counterparty_bank: str | None = None
    category_id: str | None = None
    category_name: str | None = None
    applied_rule_id: str | None = None
    applied_rule_name: str | None = None
    original_payee: str | None = None
    legacy_import_ids: list[str] = field(default_factory=list)
    legacy_exact_import_ids: list[str] = field(default_factory=list)

    @property
    def milliunits(self) -> int:
        return amount_to_milliunits(self.amount)

    def to_ynab_payload(
        self,
        *,
        account_id: str,
        transfer_payee_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "account_id": account_id,
            "date": self.booking_date.isoformat(),
            "amount": self.milliunits,
            "memo": truncate(self.memo, 500),
            "cleared": "cleared",
            "approved": False,
            "import_id": self.import_id,
        }
        if transfer_payee_id:
            payload["payee_id"] = transfer_payee_id
        else:
            payload["payee_name"] = self.payee[:200]
            payload["category_id"] = self.category_id
        return {key: value for key, value in payload.items() if value is not None}

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "statement_id": self.statement_id,
            "iban": self.iban,
            "currency": self.currency,
            "booking_date": self.booking_date.isoformat(),
            "value_date": self.value_date.isoformat() if self.value_date else None,
            "amount": str(self.amount),
            "payee": self.payee,
            "memo": self.memo,
            "source_ref": self.source_ref,
            "import_id": self.import_id,
            "sequence": self.sequence,
            "bank_code": self.bank_code,
            "counterparty_name": self.counterparty_name,
            "counterparty_iban": self.counterparty_iban,
            "counterparty_bank": self.counterparty_bank,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "applied_rule_id": self.applied_rule_id,
            "applied_rule_name": self.applied_rule_name,
            "original_payee": self.original_payee,
            "legacy_import_ids": self.legacy_import_ids,
            "legacy_exact_import_ids": self.legacy_exact_import_ids,
            "milliunits": self.milliunits,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BankTransaction":
        return cls(
            uid=data["uid"],
            statement_id=data["statement_id"],
            iban=data["iban"],
            currency=data["currency"],
            booking_date=date.fromisoformat(data["booking_date"]),
            value_date=date.fromisoformat(data["value_date"]) if data.get("value_date") else None,
            amount=Decimal(data["amount"]),
            payee=data["payee"],
            memo=data.get("memo"),
            source_ref=data.get("source_ref"),
            import_id=data["import_id"],
            sequence=int(data["sequence"]),
            bank_code=data.get("bank_code"),
            counterparty_name=data.get("counterparty_name"),
            counterparty_iban=data.get("counterparty_iban"),
            counterparty_bank=data.get("counterparty_bank"),
            category_id=data.get("category_id"),
            category_name=data.get("category_name"),
            applied_rule_id=data.get("applied_rule_id"),
            applied_rule_name=data.get("applied_rule_name"),
            original_payee=data.get("original_payee"),
            legacy_import_ids=list(data.get("legacy_import_ids") or []),
            legacy_exact_import_ids=list(data.get("legacy_exact_import_ids") or []),
        )


@dataclass
class BankStatement:
    statement_id: str
    iban: str
    currency: str
    owner_name: str | None
    bank_name: str | None
    period_start: date | None
    period_end: date | None
    opening_balance: Balance | None
    closing_balance: Balance | None
    transactions: list[BankTransaction] = field(default_factory=list)

    @property
    def movement_total(self) -> Decimal:
        return sum((tx.amount for tx in self.transactions), Decimal("0"))

    @property
    def balances_reconcile(self) -> bool | None:
        if not self.opening_balance or not self.closing_balance:
            return None
        expected = self.opening_balance.signed_amount + self.movement_total
        return expected == self.closing_balance.signed_amount

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "iban": self.iban,
            "currency": self.currency,
            "owner_name": self.owner_name,
            "bank_name": self.bank_name,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "opening_balance": self.opening_balance.to_dict() if self.opening_balance else None,
            "closing_balance": self.closing_balance.to_dict() if self.closing_balance else None,
            "movement_total": str(self.movement_total),
            "balances_reconcile": self.balances_reconcile,
            "transactions": [tx.to_dict() for tx in self.transactions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BankStatement":
        return cls(
            statement_id=data["statement_id"],
            iban=data["iban"],
            currency=data["currency"],
            owner_name=data.get("owner_name"),
            bank_name=data.get("bank_name"),
            period_start=date.fromisoformat(data["period_start"]) if data.get("period_start") else None,
            period_end=date.fromisoformat(data["period_end"]) if data.get("period_end") else None,
            opening_balance=Balance.from_dict(data["opening_balance"]) if data.get("opening_balance") else None,
            closing_balance=Balance.from_dict(data["closing_balance"]) if data.get("closing_balance") else None,
            transactions=[BankTransaction.from_dict(tx) for tx in data.get("transactions", [])],
        )


@dataclass
class ParseResult:
    statements: list[BankStatement]
    skipped_entries: int = 0

    @property
    def transactions(self) -> list[BankTransaction]:
        return [tx for statement in self.statements for tx in statement.transactions]

    @property
    def ibans(self) -> list[str]:
        return sorted({statement.iban for statement in self.statements})

    def duplicate_import_ids_by_iban(self) -> dict[str, list[str]]:
        seen: dict[tuple[str, str], int] = {}
        for tx in self.transactions:
            key = (tx.iban, tx.import_id)
            seen[key] = seen.get(key, 0) + 1
        duplicates: dict[str, list[str]] = {}
        for (iban, import_id), count in seen.items():
            if count > 1:
                duplicates.setdefault(iban, []).append(import_id)
        return {iban: sorted(ids) for iban, ids in duplicates.items()}

    def to_dict(self) -> dict[str, Any]:
        return {
            "skipped_entries": self.skipped_entries,
            "statements": [statement.to_dict() for statement in self.statements],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParseResult":
        return cls(
            statements=[BankStatement.from_dict(item) for item in data.get("statements", [])],
            skipped_entries=int(data.get("skipped_entries", 0)),
        )
