from __future__ import annotations

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


def parse_upload(filename: str, content: bytes, *, target_currency: str = "CHF") -> ParseResult:
    suffix = Path(filename).suffix.lower()
    if suffix != ".xml":
        raise UnsupportedFormatError("Only CAMT.053 XML files are supported in this version.")
    return parse_camt(content, target_currency=target_currency)


def parse_camt(content: bytes, *, target_currency: str = "CHF") -> ParseResult:
    try:
        root = ElementTree.fromstring(content)
    except Exception as exc:  # defusedxml and ElementTree expose several parse exceptions.
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
        statement, skipped = _parse_statement(element, statement_index, target_currency=target_currency)
        statements.append(statement)
        skipped_entries += skipped

    result = ParseResult(statements=statements, skipped_entries=skipped_entries)
    duplicates = result.duplicate_import_ids_by_iban()
    if duplicates:
        details = ", ".join(f"{iban}: {', '.join(ids)}" for iban, ids in duplicates.items())
        raise CamtParseError(f"Duplicate bank transaction references would create duplicate import IDs: {details}")
    return result


def _parse_statement(element: ElementTree.Element, statement_index: int, *, target_currency: str) -> tuple[BankStatement, int]:
    statement_id = _text(element, "Id") or f"statement-{statement_index}"
    iban = _text(element, "Acct/Id/IBAN")
    if not iban:
        raise CamtParseError(f"Statement {statement_id} has no IBAN.")

    currency = (_text(element, "Acct/Ccy") or "").upper()
    if not currency:
        raise CamtParseError(f"Statement {statement_id} has no account currency.")
    if currency != target_currency.upper():
        raise CamtParseError(f"Statement {statement_id} uses {currency}; only {target_currency.upper()} is supported.")

    owner_name = _text(element, "Acct/Ownr/Nm")
    bank_name = _text(element, "Acct/Svcr/FinInstnId/Nm")
    period_start = _date_text(_text(element, "FrToDt/FrDtTm") or _text(element, "FrToDt/FrDt"))
    period_end = _date_text(_text(element, "FrToDt/ToDtTm") or _text(element, "FrToDt/ToDt"))
    balances = [_parse_balance(balance, fallback_currency=currency) for balance in element.findall("Bal")]
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
        pending.extend(_parse_entry_items(entry, statement_id=statement_id, iban=iban, currency=currency, sequence=sequence))

    occurrence_by_key: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    for item in pending:
        source_ref = item["source_ref"]
        amount = item["amount"]
        booking_date = item["booking_date"]
        payee = item["payee"]
        memo = item["memo"]
        assert isinstance(source_ref, str | None)
        assert isinstance(amount, Decimal)
        assert isinstance(booking_date, date)
        assert isinstance(payee, str)
        assert isinstance(memo, str | None)
        key = (iban, booking_date.isoformat(), str(amount), payee, memo or "")
        occurrence_by_key[key] += 1
        import_id = make_import_id(
            iban=iban,
            source_ref=source_ref,
            booking_date=booking_date,
            amount=amount,
            payee=payee,
            memo=memo,
            occurrence=occurrence_by_key[key],
        )
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
        raise CamtParseError(f"Entry {sequence} in {statement_id} uses {entry_currency}, expected {currency}.")

    try:
        amount = Decimal(amount_el.text)
    except InvalidOperation as exc:
        raise CamtParseError(f"Entry {sequence} in {statement_id} has an invalid amount.") from exc

    indicator = (_text(entry, "CdtDbtInd") or "").upper()
    if indicator not in {"CRDT", "DBIT"}:
        raise CamtParseError(f"Entry {sequence} in {statement_id} has no credit/debit indicator.")
    signed_amount = amount if indicator == "CRDT" else -amount
    if (_text(entry, "RvslInd") or "").lower() == "true":
        signed_amount = -signed_amount

    booking_date = _date_text(_text(entry, "BookgDt/Dt") or _text(entry, "BookgDt/DtTm"))
    if not booking_date:
        raise CamtParseError(f"Entry {sequence} in {statement_id} has no booking date.")

    source_ref = clean_source_ref(_text(entry, "AcctSvcrRef"))
    bank_code = _bank_code(entry)
    description = _entry_description(entry)
    details = entry.findall("./NtryDtls/TxDtls")

    split_items = _split_detail_items(
        details,
        statement_id=statement_id,
        iban=iban,
        currency=currency,
        sequence=sequence,
        booking_date=booking_date,
        value_date=_date_text(_text(entry, "ValDt/Dt") or _text(entry, "ValDt/DtTm")),
        entry_description=description,
        entry_source_ref=source_ref,
        bank_code=bank_code,
        fallback_indicator=indicator,
        expected_amount=signed_amount,
    )
    if split_items:
        return split_items

    payee = payee_from_description(description)
    memo = truncate(description, 500)
    if details:
        detail = details[0]
        counterparty = _counterparty_name(detail, signed_amount)
        if counterparty and _is_generic_description(description):
            payee = counterparty[:200]
        memo = _detail_memo(description, detail, signed_amount)

    return [
        {
            "sequence": sequence,
            "iban": iban,
            "currency": currency,
            "booking_date": booking_date,
            "value_date": _date_text(_text(entry, "ValDt/Dt") or _text(entry, "ValDt/DtTm")),
            "amount": signed_amount,
            "payee": payee,
            "memo": memo,
            "source_ref": source_ref,
            "bank_code": bank_code,
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
        counterparty = _counterparty_name(detail, amount)
        source_ref = _detail_source_ref(detail, entry_source_ref=entry_source_ref, detail_index=detail_index)
        items.append(
            {
                "sequence": sequence * 1000 + detail_index,
                "iban": iban,
                "currency": currency,
                "booking_date": booking_date,
                "value_date": value_date,
                "amount": amount,
                "payee": (counterparty or payee_from_description(entry_description))[:200],
                "memo": _detail_memo(entry_description, detail, amount),
                "source_ref": source_ref,
                "bank_code": bank_code,
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
        raise CamtParseError(f"Detail {detail_index} in entry {sequence} of {statement_id} has an invalid amount.") from exc
    indicator = (_text(detail, "CdtDbtInd") or fallback_indicator).upper()
    if indicator not in {"CRDT", "DBIT"}:
        raise CamtParseError(f"Detail {detail_index} in entry {sequence} of {statement_id} has no credit/debit indicator.")
    return amount if indicator == "CRDT" else -amount


def _parse_balance(element: ElementTree.Element, *, fallback_currency: str) -> Balance:
    kind = _text(element, "Tp/CdOrPrtry/Cd") or _text(element, "Tp/CdOrPrtry/Prtry") or "UNKNOWN"
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
    return Balance(kind=kind, amount=amount, indicator=indicator, balance_date=balance_date, currency=currency)


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


def _counterparty_name(detail: ElementTree.Element, signed_amount: Decimal) -> str | None:
    if signed_amount < 0:
        paths = (
            "RltdPties/Cdtr/Pty/Nm",
            "RltdPties/UltmtCdtr/Pty/Nm",
            "RltdPties/Dbtr/Pty/Nm",
        )
    else:
        paths = (
            "RltdPties/Dbtr/Pty/Nm",
            "RltdPties/UltmtDbtr/Pty/Nm",
            "RltdPties/Cdtr/Pty/Nm",
        )
    for path in paths:
        value = _text(detail, path)
        if value:
            return normalize_whitespace(value)
    return None


def _detail_source_ref(detail: ElementTree.Element, *, entry_source_ref: str | None, detail_index: int) -> str | None:
    for path in (
        "RmtInf/Strd/CdtrRefInf/Ref",
        "Refs/InstrId",
        "Refs/UETR",
        "Refs/EndToEndId",
    ):
        value = clean_source_ref(_text(detail, path))
        if value:
            return value
    if entry_source_ref:
        return f"{entry_source_ref}:{detail_index}"
    return None


def _detail_memo(base_description: str, detail: ElementTree.Element, signed_amount: Decimal) -> str | None:
    parts: list[str] = []
    if base_description:
        parts.append(base_description)
    for element in detail.findall("RmtInf/Ustrd"):
        if element.text:
            parts.append(element.text)
    structured_ref = _text(detail, "RmtInf/Strd/CdtrRefInf/Ref")
    if structured_ref:
        parts.append(f"Reference: {structured_ref}")
    end_to_end = clean_source_ref(_text(detail, "Refs/EndToEndId"))
    if end_to_end:
        parts.append(f"End-to-end: {end_to_end}")
    counterparty_iban = _text(detail, "RltdPties/CdtrAcct/Id/IBAN") if signed_amount < 0 else _text(detail, "RltdPties/DbtrAcct/Id/IBAN")
    if counterparty_iban:
        parts.append(f"Counterparty IBAN: {counterparty_iban}")
    return truncate("\n".join(parts), 500)


def _is_generic_description(description: str) -> bool:
    normalized = normalize_whitespace(description).casefold()
    return normalized in {"paiement groupé", "paiement groupe", "ordre permanent"} or normalized.startswith("ordre permanent ")


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


def _strip_namespaces(root: ElementTree.Element) -> None:
    for element in root.iter():
        if "}" in element.tag:
            element.tag = element.tag.rsplit("}", 1)[1]
