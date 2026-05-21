from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

from defusedxml import ElementTree

from .models import (
    Balance,
    BankStatement,
    BankTransaction,
    ParseResult,
    clean_source_ref,
    compact_whitespace,
    make_import_id,
    normalize_whitespace,
    payee_from_description,
    truncate,
)


class CamtError(ValueError):
    pass


class UnsupportedFormatError(CamtError):
    pass


class CamtParseError(CamtError):
    pass


CSV_REQUIRED_COLUMNS = {
    "Date",
    "Amount",
    "Original amount",
    "Original currency",
    "Exchange rate",
    "Description",
    "Subject",
    "Category",
    "Tags",
    "Wise",
    "Spaces",
}

CARD_PURCHASE_PATTERN = re.compile(
    r"(?P<purchase_type>Achat online|Achat)\s+"
    r"(?P<merchant>.+?)\s+"
    r"(?P<date>\d{2}\.\d{2}\.\d{4}),\s*"
    r"(?P<time>\d{2}:\d{2}),\s*"
    r"No carte\b(?P<card>.*?)(?:\s+"
    r"(?P<foreign_currency>[A-Z]{3})\s+"
    r"(?P<foreign_amount>\d+(?:[.,]\d+)?),\s*"
    r"taux de change\s+(?P<exchange_rate>\d+(?:[.,]\d+)?))?"
    r"\s*",
    flags=re.IGNORECASE,
)
TWINT_PAYMENT_PATTERN = re.compile(
    r"(?P<payment_type>Achat|Paiement|Cr[ée]dit)\s+TWINT\s+"
    r"(?P<merchant>.+?)\s+"
    r"(?P<date>\d{2}\.\d{2}\.\d{4}),\s*"
    r"(?P<time>\d{2}:\d{2})(?:,?\s*.*)?"
    r"\s*",
    flags=re.IGNORECASE,
)


def parse_upload(
    filename: str,
    content: bytes,
    *,
    target_currency: str = "CHF",
    csv_account_key: str | None = None,
) -> ParseResult:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xml":
        return parse_camt(content, target_currency=target_currency)
    if suffix == ".csv":
        if not csv_account_key:
            raise CamtParseError("CSV uploads require a selected budget account.")
        return parse_csv_export(
            filename,
            content,
            account_iban=csv_account_key,
            target_currency=target_currency,
        )
    if suffix == ".mt940":
        raise UnsupportedFormatError(
            "MT940 files are not supported. Use CAMT.053 XML or the supported CSV export."
        )
    raise UnsupportedFormatError(
        "Only CAMT.053 XML and the supported CSV export are accepted."
    )


def parse_csv_export(
    filename: str, content: bytes, *, account_iban: str, target_currency: str = "CHF"
) -> ParseResult:
    account_iban = _normalize_account_key(account_iban)
    if not account_iban:
        raise CamtParseError("CSV target account key is required.")
    text = _decode_csv(content)
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=";", quotechar='"')
        fieldnames = set(reader.fieldnames or [])
        missing = sorted(CSV_REQUIRED_COLUMNS - fieldnames)
        if missing:
            raise CamtParseError(
                f"CSV export is missing required columns: {', '.join(missing)}"
            )
        raw_rows = [
            row
            for row in reader
            if any((value or "").strip() for value in row.values())
        ]
    except csv.Error as exc:
        raise CamtParseError("The uploaded CSV could not be parsed.") from exc

    statement_id = f"CSV:{Path(filename).stem}"
    transactions: list[BankTransaction] = []
    occurrence_by_key: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    dates: list[date] = []
    for sequence, row in enumerate(raw_rows, start=1):
        booking_date = _parse_csv_date(row.get("Date"), sequence=sequence)
        dates.append(booking_date)
        amount = _parse_csv_amount(row.get("Amount"), sequence=sequence)
        description = normalize_whitespace(row.get("Description"))
        payee = payee_from_description(description)
        memo = _csv_memo(row)
        key = (account_iban, booking_date.isoformat(), str(amount), payee, memo or "")
        occurrence_by_key[key] += 1
        import_id = make_import_id(
            iban=account_iban,
            source_ref=None,
            booking_date=booking_date,
            amount=amount,
            payee=payee,
            memo=memo,
            occurrence=occurrence_by_key[key],
        )
        transactions.append(
            BankTransaction(
                uid=f"{statement_id}:{sequence}",
                statement_id=statement_id,
                iban=account_iban,
                currency=target_currency.upper(),
                booking_date=booking_date,
                value_date=booking_date,
                amount=amount,
                payee=payee,
                memo=memo,
                source_ref=None,
                import_id=import_id,
                sequence=sequence,
                bank_code="CSV",
                counterparty_name=description or payee,
            )
        )

    statement = BankStatement(
        statement_id=statement_id,
        iban=account_iban,
        currency=target_currency.upper(),
        owner_name=None,
        bank_name="CSV export",
        period_start=min(dates) if dates else None,
        period_end=max(dates) if dates else None,
        opening_balance=None,
        closing_balance=None,
        transactions=transactions,
    )
    result = ParseResult(statements=[statement], skipped_entries=0)
    duplicates = result.duplicate_import_ids_by_iban()
    if duplicates:
        details = ", ".join(
            f"{iban}: {', '.join(ids)}" for iban, ids in duplicates.items()
        )
        raise CamtParseError(
            f"Duplicate CSV rows would create duplicate import IDs: {details}"
        )
    return result


def parse_camt(content: bytes, *, target_currency: str = "CHF") -> ParseResult:
    try:
        root = ElementTree.fromstring(content)
    except (
        Exception
    ) as exc:  # defusedxml and ElementTree expose several parse exceptions.
        raise CamtParseError("The uploaded file is not valid XML.") from exc

    _strip_namespaces(root)
    if root.tag != "Document":
        raise CamtParseError("The XML root is not a CAMT Document.")

    statements: list[BankStatement] = []
    skipped_entries = 0
    statement_elements = list(root.findall(".//Stmt"))
    if not statement_elements:
        raise CamtParseError("No CAMT statement blocks were found.")

    for statement_index, element in enumerate(statement_elements, start=1):
        statement, skipped = _parse_statement(
            element, statement_index, target_currency=target_currency
        )
        statements.append(statement)
        skipped_entries += skipped

    result = ParseResult(statements=statements, skipped_entries=skipped_entries)
    duplicates = result.duplicate_import_ids_by_iban()
    if duplicates:
        details = ", ".join(
            f"{iban}: {', '.join(ids)}" for iban, ids in duplicates.items()
        )
        raise CamtParseError(
            f"Duplicate bank transaction references would create duplicate import IDs: {details}"
        )
    return result


def _parse_statement(
    element: ElementTree.Element, statement_index: int, *, target_currency: str
) -> tuple[BankStatement, int]:
    statement_id = _text(element, "Id") or f"statement-{statement_index}"
    iban = _text(element, "Acct/Id/IBAN")
    if not iban:
        raise CamtParseError(f"Statement {statement_id} has no IBAN.")

    currency = (_text(element, "Acct/Ccy") or "").upper()
    if not currency:
        raise CamtParseError(f"Statement {statement_id} has no account currency.")
    if currency != target_currency.upper():
        raise CamtParseError(
            f"Statement {statement_id} uses {currency}; only {target_currency.upper()} is supported."
        )

    owner_name = _text(element, "Acct/Ownr/Nm")
    bank_name = _text(element, "Acct/Svcr/FinInstnId/Nm")
    period_start = _date_text(
        _text(element, "FrToDt/FrDtTm") or _text(element, "FrToDt/FrDt")
    )
    period_end = _date_text(
        _text(element, "FrToDt/ToDtTm") or _text(element, "FrToDt/ToDt")
    )
    balances = [
        _parse_balance(balance, fallback_currency=currency)
        for balance in element.findall("Bal")
    ]
    opening_balance = _first_balance(balances, "OPBD")
    closing_balance = _first_balance(balances, "CLBD")

    transactions: list[BankTransaction] = []
    pending: list[dict[str, object]] = []
    skipped = 0
    for sequence, entry in enumerate(element.findall("Ntry"), start=1):
        status = (_text(entry, "Sts/Cd") or _text(entry, "Sts") or "").upper()
        if status and status != "BOOK":
            skipped += 1
            continue
        pending.extend(
            _parse_entry_items(
                entry,
                statement_id=statement_id,
                iban=iban,
                currency=currency,
                sequence=sequence,
            )
        )

    occurrence_by_key: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    bank_occurrence_by_key: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    for item in pending:
        source_ref = item["source_ref"]
        amount = item["amount"]
        booking_date = item["booking_date"]
        payee = item["payee"]
        memo = item["memo"]
        counterparty_name = item["counterparty_name"]
        counterparty_iban = item["counterparty_iban"]
        counterparty_bank = item["counterparty_bank"]
        legacy_source_refs = item.get("legacy_source_refs") or []
        legacy_exact_source_refs = item.get("legacy_exact_source_refs") or []
        legacy_booking_dates = item.get("legacy_booking_dates") or []
        bank_booking_date = item.get("bank_booking_date") or booking_date
        assert isinstance(source_ref, str | None)
        assert isinstance(amount, Decimal)
        assert isinstance(booking_date, date)
        assert isinstance(payee, str)
        assert isinstance(memo, str | None)
        assert isinstance(counterparty_name, str | None)
        assert isinstance(counterparty_iban, str | None)
        assert isinstance(counterparty_bank, str | None)
        assert isinstance(legacy_source_refs, list)
        assert isinstance(legacy_exact_source_refs, list)
        assert isinstance(legacy_booking_dates, list)
        assert isinstance(bank_booking_date, date)
        key = (iban, booking_date.isoformat(), str(amount), payee, memo or "")
        occurrence_by_key[key] += 1
        bank_key = (iban, bank_booking_date.isoformat(), str(amount), payee, memo or "")
        bank_occurrence_by_key[bank_key] += 1
        import_id = make_import_id(
            iban=iban,
            source_ref=source_ref,
            booking_date=booking_date,
            amount=amount,
            payee=payee,
            memo=memo,
            occurrence=occurrence_by_key[key],
        )
        legacy_import_ids = [
            make_import_id(
                iban=iban,
                source_ref=legacy_source_ref,
                booking_date=booking_date,
                amount=amount,
                payee=payee,
                memo=memo,
                occurrence=occurrence_by_key[key],
            )
            for legacy_source_ref in legacy_source_refs
            if isinstance(legacy_source_ref, str)
        ]
        if source_ref is None:
            for legacy_booking_date in legacy_booking_dates:
                if not isinstance(legacy_booking_date, date):
                    continue
                legacy_key = (
                    iban,
                    legacy_booking_date.isoformat(),
                    str(amount),
                    payee,
                    memo or "",
                )
                legacy_import_id = make_import_id(
                    iban=iban,
                    source_ref=None,
                    booking_date=legacy_booking_date,
                    amount=amount,
                    payee=payee,
                    memo=memo,
                    occurrence=bank_occurrence_by_key[legacy_key],
                )
                if (
                    legacy_import_id != import_id
                    and legacy_import_id not in legacy_import_ids
                ):
                    legacy_import_ids.append(legacy_import_id)
        legacy_exact_import_ids = [
            make_import_id(
                iban=iban,
                source_ref=legacy_source_ref,
                booking_date=booking_date,
                amount=amount,
                payee=payee,
                memo=memo,
                occurrence=occurrence_by_key[key],
            )
            for legacy_source_ref in legacy_exact_source_refs
            if isinstance(legacy_source_ref, str)
        ]
        transactions.append(
            BankTransaction(
                uid=f"{statement_id}:{item['sequence']}",
                statement_id=statement_id,
                iban=iban,
                currency=currency,
                booking_date=booking_date,
                value_date=item["value_date"],  # type: ignore[arg-type]
                amount=amount,
                payee=payee,
                memo=memo,
                source_ref=source_ref,
                import_id=import_id,
                sequence=int(item["sequence"]),
                bank_code=item["bank_code"],  # type: ignore[arg-type]
                counterparty_name=counterparty_name,
                counterparty_iban=counterparty_iban,
                counterparty_bank=counterparty_bank,
                legacy_import_ids=legacy_import_ids,
                legacy_exact_import_ids=legacy_exact_import_ids,
            )
        )

    statement = BankStatement(
        statement_id=statement_id,
        iban=iban,
        currency=currency,
        owner_name=owner_name,
        bank_name=bank_name,
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_balance,
        closing_balance=closing_balance,
        transactions=transactions,
    )
    return statement, skipped


def _parse_entry_items(
    entry: ElementTree.Element,
    *,
    statement_id: str,
    iban: str,
    currency: str,
    sequence: int,
) -> list[dict[str, object]]:
    amount_el = entry.find("Amt")
    if amount_el is None or not amount_el.text:
        raise CamtParseError(f"Entry {sequence} in {statement_id} has no amount.")
    entry_currency = (amount_el.attrib.get("Ccy") or currency).upper()
    if entry_currency != currency:
        raise CamtParseError(
            f"Entry {sequence} in {statement_id} uses {entry_currency}, expected {currency}."
        )

    try:
        amount = Decimal(amount_el.text)
    except InvalidOperation as exc:
        raise CamtParseError(
            f"Entry {sequence} in {statement_id} has an invalid amount."
        ) from exc

    indicator = (_text(entry, "CdtDbtInd") or "").upper()
    if indicator not in {"CRDT", "DBIT"}:
        raise CamtParseError(
            f"Entry {sequence} in {statement_id} has no credit/debit indicator."
        )
    signed_amount = amount if indicator == "CRDT" else -amount
    if (_text(entry, "RvslInd") or "").lower() == "true":
        signed_amount = -signed_amount

    booking_date = _date_text(
        _text(entry, "BookgDt/Dt") or _text(entry, "BookgDt/DtTm")
    )
    if not booking_date:
        raise CamtParseError(f"Entry {sequence} in {statement_id} has no booking date.")

    source_ref = clean_source_ref(_text(entry, "AcctSvcrRef"))
    bank_code = _bank_code(entry)
    description = _entry_description(entry)
    details = entry.findall("./NtryDtls/TxDtls")

    bank_value_date = _date_text(_text(entry, "ValDt/Dt") or _text(entry, "ValDt/DtTm"))
    split_items = _split_detail_items(
        details,
        statement_id=statement_id,
        iban=iban,
        currency=currency,
        sequence=sequence,
        booking_date=booking_date,
        value_date=bank_value_date,
        entry_description=description,
        entry_source_ref=source_ref,
        bank_code=bank_code,
        fallback_indicator=indicator,
        expected_amount=signed_amount,
    )
    if split_items:
        return split_items

    payee = payee_from_description(description)
    memo = _entry_memo(description)
    transaction_date = _transaction_booking_date(booking_date, description)
    legacy_booking_dates = [booking_date] if transaction_date != booking_date else []
    counterparty = {"name": None, "iban": None, "bank": None}
    if details:
        detail = details[0]
        counterparty = _counterparty_info(detail, signed_amount)
        if counterparty["name"] and _is_generic_description(description):
            payee = counterparty["name"][:200]
        memo = _detail_memo(description, detail, signed_amount)

    return [
        {
            "sequence": sequence,
            "iban": iban,
            "currency": currency,
            "booking_date": transaction_date,
            "bank_booking_date": booking_date,
            "value_date": bank_value_date,
            "amount": signed_amount,
            "payee": payee,
            "memo": memo,
            "source_ref": source_ref,
            "bank_code": bank_code,
            "counterparty_name": counterparty["name"],
            "counterparty_iban": counterparty["iban"],
            "counterparty_bank": counterparty["bank"],
            "legacy_booking_dates": legacy_booking_dates,
        }
    ]


def _split_detail_items(
    details: list[ElementTree.Element],
    *,
    statement_id: str,
    iban: str,
    currency: str,
    sequence: int,
    booking_date: date,
    value_date: date | None,
    entry_description: str,
    entry_source_ref: str | None,
    bank_code: str | None,
    fallback_indicator: str,
    expected_amount: Decimal,
) -> list[dict[str, object]]:
    if len(details) <= 1:
        return []

    parsed_details: list[tuple[ElementTree.Element, Decimal]] = []
    total = Decimal("0")
    for detail_index, detail in enumerate(details, start=1):
        amount = _detail_signed_amount(
            detail,
            fallback_indicator=fallback_indicator,
            currency=currency,
            statement_id=statement_id,
            sequence=sequence,
            detail_index=detail_index,
        )
        if amount is None:
            return []
        parsed_details.append((detail, amount))
        total += amount

    if total != expected_amount:
        return []

    items: list[dict[str, object]] = []
    for detail_index, (detail, amount) in enumerate(parsed_details, start=1):
        counterparty = _counterparty_info(detail, amount)
        source_ref = _detail_source_ref(
            detail, entry_source_ref=entry_source_ref, detail_index=detail_index
        )
        legacy_source_refs = _legacy_detail_source_refs(
            detail, current_source_ref=source_ref
        )
        legacy_exact_source_refs = _legacy_exact_detail_source_refs(
            detail, current_source_ref=source_ref
        )
        items.append(
            {
                "sequence": sequence * 1000 + detail_index,
                "iban": iban,
                "currency": currency,
                "booking_date": booking_date,
                "value_date": value_date,
                "amount": amount,
                "payee": (
                    counterparty["name"] or payee_from_description(entry_description)
                )[:200],
                "memo": _detail_memo(entry_description, detail, amount),
                "source_ref": source_ref,
                "legacy_source_refs": legacy_source_refs,
                "legacy_exact_source_refs": legacy_exact_source_refs,
                "bank_code": bank_code,
                "counterparty_name": counterparty["name"],
                "counterparty_iban": counterparty["iban"],
                "counterparty_bank": counterparty["bank"],
            }
        )
    return items


def _detail_signed_amount(
    detail: ElementTree.Element,
    *,
    fallback_indicator: str,
    currency: str,
    statement_id: str,
    sequence: int,
    detail_index: int,
) -> Decimal | None:
    amount_el = detail.find("Amt")
    if amount_el is None or not amount_el.text:
        return None
    detail_currency = (amount_el.attrib.get("Ccy") or currency).upper()
    if detail_currency != currency:
        raise CamtParseError(
            f"Detail {detail_index} in entry {sequence} of {statement_id} uses {detail_currency}, expected {currency}."
        )
    try:
        amount = Decimal(amount_el.text)
    except InvalidOperation as exc:
        raise CamtParseError(
            f"Detail {detail_index} in entry {sequence} of {statement_id} has an invalid amount."
        ) from exc
    indicator = (_text(detail, "CdtDbtInd") or fallback_indicator).upper()
    if indicator not in {"CRDT", "DBIT"}:
        raise CamtParseError(
            f"Detail {detail_index} in entry {sequence} of {statement_id} has no credit/debit indicator."
        )
    return amount if indicator == "CRDT" else -amount


def _parse_balance(element: ElementTree.Element, *, fallback_currency: str) -> Balance:
    kind = (
        _text(element, "Tp/CdOrPrtry/Cd")
        or _text(element, "Tp/CdOrPrtry/Prtry")
        or "UNKNOWN"
    )
    amount_el = element.find("Amt")
    if amount_el is None or not amount_el.text:
        raise CamtParseError(f"Balance {kind} has no amount.")
    currency = (amount_el.attrib.get("Ccy") or fallback_currency).upper()
    try:
        amount = Decimal(amount_el.text)
    except InvalidOperation as exc:
        raise CamtParseError(f"Balance {kind} has an invalid amount.") from exc
    indicator = (_text(element, "CdtDbtInd") or "CRDT").upper()
    balance_date = _date_text(_text(element, "Dt/Dt") or _text(element, "Dt/DtTm"))
    return Balance(
        kind=kind,
        amount=amount,
        indicator=indicator,
        balance_date=balance_date,
        currency=currency,
    )


def _decode_csv(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "iso-8859-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CamtParseError("The uploaded CSV encoding is not supported.")


def _parse_csv_date(value: str | None, *, sequence: int) -> date:
    value = normalize_whitespace(value)
    if not value:
        raise CamtParseError(f"CSV row {sequence} has no date.")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise CamtParseError(f"CSV row {sequence} has an invalid date.") from exc


def _parse_csv_amount(value: str | None, *, sequence: int) -> Decimal:
    value = normalize_whitespace(value)
    if not value:
        raise CamtParseError(f"CSV row {sequence} has no amount.")
    try:
        return Decimal(value.replace("'", "").replace(",", "."))
    except InvalidOperation as exc:
        raise CamtParseError(f"CSV row {sequence} has an invalid amount.") from exc


def _csv_memo(row: dict[str, str | None]) -> str | None:
    parts: list[str] = []
    description = normalize_whitespace(row.get("Description"))
    if description:
        parts.append(description)
    subject = _csv_optional_value(row.get("Subject"))
    if subject:
        parts.append(subject)
    original_amount = _csv_optional_value(row.get("Original amount"))
    original_currency = _csv_optional_value(row.get("Original currency"))
    if original_amount and original_currency:
        parts.append(f"Original amount: {original_amount} {original_currency}")
    return _memo_from_parts(parts)


def _csv_optional_value(value: str | None) -> str:
    normalized = normalize_whitespace(value)
    if normalized.casefold() == "no":
        return ""
    return normalized


def _first_balance(balances: Iterable[Balance], kind: str) -> Balance | None:
    for balance in balances:
        if balance.kind == kind:
            return balance
    return None


def _entry_description(entry: ElementTree.Element) -> str:
    direct = _text(entry, "AddtlNtryInf")
    if direct:
        return normalize_whitespace(direct)

    texts: list[str] = []
    for path in (
        ".//RmtInf/Ustrd",
        ".//RmtInf/Strd/CdtrRefInf/Ref",
        ".//TxDtls/AddtlTxInf",
        ".//NtryDtls/Btch/PmtInfId",
    ):
        for element in entry.findall(path):
            if element.text:
                texts.append(element.text)
    return normalize_whitespace("\n".join(texts))


def _counterparty_info(
    detail: ElementTree.Element, signed_amount: Decimal
) -> dict[str, str | None]:
    if signed_amount < 0:
        name_paths = (
            "RltdPties/Cdtr/Pty/Nm",
            "RltdPties/UltmtCdtr/Pty/Nm",
        )
        iban_paths = (
            "RltdPties/CdtrAcct/Id/IBAN",
            "RltdPties/UltmtCdtrAcct/Id/IBAN",
        )
        bank_paths = ("RltdAgts/CdtrAgt/FinInstnId/Nm",)
    else:
        name_paths = (
            "RltdPties/Dbtr/Pty/Nm",
            "RltdPties/UltmtDbtr/Pty/Nm",
        )
        iban_paths = (
            "RltdPties/DbtrAcct/Id/IBAN",
            "RltdPties/UltmtDbtrAcct/Id/IBAN",
        )
        bank_paths = ("RltdAgts/DbtrAgt/FinInstnId/Nm",)
    return {
        "name": _first_text(detail, name_paths, normalize=True),
        "iban": _iban_text(_first_text(detail, iban_paths)),
        "bank": _first_text(detail, bank_paths, normalize=True),
    }


def _detail_source_ref(
    detail: ElementTree.Element, *, entry_source_ref: str | None, detail_index: int
) -> str | None:
    if entry_source_ref:
        return f"{entry_source_ref}.{detail_index}"
    for path in (
        "Refs/AcctSvcrRef",
        "Refs/TxId",
        "Refs/UETR",
        "Refs/InstrId",
        "Refs/EndToEndId",
    ):
        value = clean_source_ref(_text(detail, path))
        if value:
            return value
    return None


def _legacy_detail_source_refs(
    detail: ElementTree.Element, *, current_source_ref: str | None
) -> list[str]:
    refs: list[str] = []
    for path in (
        "Refs/InstrId",
        "Refs/UETR",
        "Refs/EndToEndId",
    ):
        value = clean_source_ref(_text(detail, path))
        if value and value != current_source_ref and value not in refs:
            refs.append(value)
    return refs


def _legacy_exact_detail_source_refs(
    detail: ElementTree.Element, *, current_source_ref: str | None
) -> list[str]:
    structured_ref = clean_source_ref(_text(detail, "RmtInf/Strd/CdtrRefInf/Ref"))
    if structured_ref and structured_ref != current_source_ref:
        return [structured_ref]
    return []


def _detail_memo(
    base_description: str, detail: ElementTree.Element, signed_amount: Decimal
) -> str | None:
    parts: list[str] = []
    counterparty = _counterparty_info(detail, signed_amount)
    if _should_include_base_description(base_description, counterparty["name"]):
        parts.append(_entry_memo(base_description) or base_description)
    for element in detail.findall("RmtInf/Ustrd"):
        if element.text:
            parts.append(element.text)
    structured_ref = _text(detail, "RmtInf/Strd/CdtrRefInf/Ref")
    if structured_ref:
        parts.append(f"Ref: {structured_ref}")
    end_to_end = clean_source_ref(_text(detail, "Refs/EndToEndId"))
    if end_to_end:
        parts.append(f"E2E: {end_to_end}")
    counterparty_iban = (
        _text(detail, "RltdPties/CdtrAcct/Id/IBAN")
        if signed_amount < 0
        else _text(detail, "RltdPties/DbtrAcct/Id/IBAN")
    )
    if counterparty_iban:
        parts.append(f"IBAN: {_iban_text(counterparty_iban) or counterparty_iban}")
    counterparty_bank = counterparty["bank"]
    if counterparty_bank:
        parts.append(f"Bank: {counterparty_bank}")
    return _memo_from_parts(parts)


def _entry_memo(description: str) -> str | None:
    card_memo = _card_purchase_memo(description)
    if card_memo:
        return truncate(card_memo, 500)
    twint_memo = _twint_payment_memo(description)
    if twint_memo:
        return truncate(twint_memo, 500)
    return truncate(description, 500)


def _card_purchase_memo(description: str) -> str | None:
    match = CARD_PURCHASE_PATTERN.fullmatch(normalize_whitespace(description))
    if not match:
        return None

    purchase_type = match.group("purchase_type").lower()

    if purchase_type == "achat online":
        label = "Online"
    else:
        label = "Card"

    return f"{label}: {match.group('date')} {match.group('time')}"


def _transaction_booking_date(bank_booking_date: date, description: str) -> date:
    payment_date = _card_payment_date(description) or _twint_payment_date(description)
    return payment_date or bank_booking_date


def _twint_payment_memo(description: str) -> str | None:
    match = TWINT_PAYMENT_PATTERN.fullmatch(normalize_whitespace(description))
    if not match:
        return None
    return f"TWINT: {match.group('date')} {match.group('time')}"


def _card_payment_date(description: str) -> date | None:
    match = CARD_PURCHASE_PATTERN.fullmatch(normalize_whitespace(description))
    return _payment_date_from_match(match)


def _twint_payment_date(description: str) -> date | None:
    match = TWINT_PAYMENT_PATTERN.fullmatch(normalize_whitespace(description))
    return _payment_date_from_match(match)


def _payment_date_from_match(match: re.Match[str] | None) -> date | None:
    if not match:
        return None
    day, month, year = match.group("date").split(".")
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _memo_from_parts(parts: list[str | None]) -> str | None:
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        value = compact_whitespace(part)
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        cleaned.append(value)
        seen.add(key)
    return truncate("; ".join(cleaned), 500)


def _should_include_base_description(
    base_description: str, counterparty_name: str | None
) -> bool:
    if not base_description:
        return False
    if _is_generic_description(base_description):
        return True
    if not counterparty_name:
        return True
    base = compact_whitespace(base_description).casefold()
    counterparty = compact_whitespace(counterparty_name).casefold()
    if not counterparty:
        return True
    return (
        counterparty not in base
        and payee_from_description(base_description).casefold() != counterparty
    )


def _is_generic_description(description: str) -> bool:
    normalized = normalize_whitespace(description).casefold()
    return normalized in {
        "paiement groupé",
        "paiement groupe",
        "ordre permanent",
    } or normalized.startswith("ordre permanent ")


def _bank_code(entry: ElementTree.Element) -> str | None:
    parts = [
        _text(entry, "BkTxCd/Domn/Cd"),
        _text(entry, "BkTxCd/Domn/Fmly/Cd"),
        _text(entry, "BkTxCd/Domn/Fmly/SubFmlyCd"),
        _text(entry, "BkTxCd/Prtry/Cd"),
    ]
    return "/".join(part for part in parts if part) or None


def _date_text(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


def _text(element: ElementTree.Element, path: str) -> str | None:
    found = element.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip()


def _first_text(
    element: ElementTree.Element, paths: Iterable[str], *, normalize: bool = False
) -> str | None:
    for path in paths:
        value = _text(element, path)
        if value:
            return normalize_whitespace(value) if normalize else value
    return None


def _iban_text(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(value.upper().split())


def _normalize_account_key(value: str) -> str:
    return "".join(value.upper().split())


def _strip_namespaces(root: ElementTree.Element) -> None:
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.rsplit("}", 1)[1]
