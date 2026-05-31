from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from inab.camt import (
    CamtParseError,
    UnsupportedFormatError,
    parse_camt,
    parse_csv_export,
    parse_upload,
)

from conftest import camt_document, entry_xml, statement_xml


def test_sample_camt_parses_and_reconciles() -> None:
    samples = sorted(Path("sample").glob("camt*.xml"))
    sample = (
        max(samples, key=lambda path: (path.stat().st_mtime, path.stat().st_size))
        if samples
        else Path("sample/camt053_001_08_ch0000000000000000000_20260519071007.xml")
    )
    if not sample.exists():
        pytest.skip("Local sample export is not present.")

    result = parse_upload(sample.name, sample.read_bytes())

    assert result.statements
    assert result.transactions
    assert all(statement.iban.startswith("CH") for statement in result.statements)
    assert all(
        statement.balances_reconcile is not False for statement in result.statements
    )
    by_account_import_ids = {(tx.iban, tx.import_id) for tx in result.transactions}
    assert len(by_account_import_ids) == len(result.transactions)
    assert all("\n" not in tx.memo for tx in result.transactions if tx.memo)


def test_multi_account_camt_groups_by_statement() -> None:
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("100.00", "DBIT", "REF1", "Transfer to savings"),
            opening="1000.00",
            closing="900.00",
        )
        + statement_xml(
            "CH222",
            entry_xml("100.00", "CRDT", "REF2", "Transfer from checking"),
            opening="500.00",
            closing="600.00",
        )
    )

    result = parse_camt(content)

    assert result.ibans == ["CH111", "CH222"]
    assert [statement.movement_total for statement in result.statements] == [
        Decimal("-100.00"),
        Decimal("100.00"),
    ]
    assert all(statement.balances_reconcile for statement in result.statements)


def test_rejects_unsupported_file_extension() -> None:
    with pytest.raises(UnsupportedFormatError):
        parse_upload("export.mt940", b"not supported")


def test_csv_upload_requires_selected_account() -> None:
    with pytest.raises(
        CamtParseError, match="CSV uploads require a selected budget account"
    ):
        parse_upload(
            "export.csv",
            b'"Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"\n',
        )


def test_supported_csv_export_parses_with_selected_account_key() -> None:
    content = b""""Date";"Amount";"Original amount";"Original currency";"Exchange rate";"Description";"Subject";"Category";"Tags";"Wise";"Spaces"
"2026-04-30";"600.00";"";"";"";"Alex Example";"Salary April";"income";"";"no";"no"
"2026-04-28";"-16.57";"-17.90";"EUR";"1.08027";"SAMPLE BISTRO";;"food";"";"no";"no"
"""

    result = parse_csv_export(
        "neon.csv", content, account_iban="CH999", target_currency="CHF"
    )

    assert result.ibans == ["CH999"]
    assert len(result.transactions) == 2
    assert result.statements[0].period_start.isoformat() == "2026-04-28"
    assert result.statements[0].period_end.isoformat() == "2026-04-30"
    assert result.statements[0].balances_reconcile is None
    assert [tx.amount for tx in result.transactions] == [
        Decimal("600.00"),
        Decimal("-16.57"),
    ]
    assert [tx.payee for tx in result.transactions] == ["Alex Example", "Sample Bistro"]
    assert result.transactions[0].import_id.startswith("INAB:")
    assert result.transactions[0].source_ref is None
    assert result.transactions[0].memo == "Alex Example; Salary April"
    assert result.transactions[1].memo == "SAMPLE BISTRO; Original amount: -17.90 EUR"


def test_rejects_non_chf_statement() -> None:
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Payee", currency="EUR"),
            currency="EUR",
        )
    )

    with pytest.raises(CamtParseError, match="only CHF is supported"):
        parse_camt(content)


def test_rejects_mixed_entry_currency() -> None:
    content = camt_document(
        statement_xml(
            "CH111", entry_xml("10.00", "DBIT", "REF1", "Payee", currency="EUR")
        )
    )

    with pytest.raises(CamtParseError, match="uses EUR, expected CHF"):
        parse_camt(content)


def test_rejects_duplicate_bank_references_for_same_iban() -> None:
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml("10.00", "DBIT", "REF1", "Payee one")
            + entry_xml(
                "20.00",
                "DBIT",
                "REF1",
                "Payee two",
                booking_date="2026-04-11",
                value_date="2026-04-11",
            ),
        )
    )

    with pytest.raises(CamtParseError, match="Duplicate bank transaction references"):
        parse_camt(content)


def test_single_generic_entry_uses_detail_counterparty_and_memo() -> None:
    entry = """
<Ntry>
  <Amt Ccy="CHF">1200.00</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-05-01</Dt></BookgDt>
  <ValDt><Dt>2026-05-01</Dt></ValDt>
  <AcctSvcrRef>ENTRYREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">1200.00</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties>
        <Dbtr><Pty><Nm>Alex Example</Nm></Pty></Dbtr>
        <Cdtr><Pty><Nm>Regie Example SA</Nm></Pty></Cdtr>
        <CdtrAcct><Id><IBAN>CH0000000000000000001</IBAN></Id></CdtrAcct>
      </RltdPties>
      <RltdAgts><CdtrAgt><FinInstnId><Nm>Example Bank AG</Nm></FinInstnId></CdtrAgt></RltdAgts>
      <RmtInf><Ustrd>Loyer mai 2026</Ustrd></RmtInf>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Ordre permanent</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="2000.00", closing="800.00")
    )

    result = parse_camt(content)
    tx = result.transactions[0]

    assert tx.payee == "Regie Example SA"
    assert tx.source_ref == "ENTRYREF"
    assert tx.import_id == "INAB:ENTRYREF"
    assert tx.memo and "Ordre permanent" in tx.memo
    assert "Loyer mai 2026" in tx.memo
    assert "IBAN: CH0000000000000000001" in tx.memo
    assert "Bank: Example Bank AG" in tx.memo
    assert tx.counterparty_name == "Regie Example SA"
    assert tx.counterparty_iban == "CH0000000000000000001"
    assert tx.counterparty_bank == "Example Bank AG"


def test_grouped_payment_splits_reconciled_transaction_details() -> None:
    entry = """
<Ntry>
  <Amt Ccy="CHF">39.10</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-05-08</Dt></BookgDt>
  <ValDt><Dt>2026-05-08</Dt></ValDt>
  <AcctSvcrRef>BATCHREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Refs><InstrId>AID-one</InstrId></Refs>
      <Amt Ccy="CHF">19.55</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Insurance Example SA</Nm></Pty></Cdtr><CdtrAcct><Id><IBAN>CH0000000000000000002</IBAN></Id></CdtrAcct></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>DETAILREFONE</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
    <TxDtls>
      <Refs><InstrId>AID-two</InstrId></Refs>
      <Amt Ccy="CHF">19.55</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Insurance Example SA</Nm></Pty></Cdtr><CdtrAcct><Id><IBAN>CH0000000000000000002</IBAN></Id></CdtrAcct></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>DETAILREFTWO</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Paiement groupé</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="100.00", closing="60.90")
    )

    result = parse_camt(content)

    assert len(result.transactions) == 2
    assert result.statements[0].movement_total == Decimal("-39.10")
    assert result.statements[0].balances_reconcile is True
    assert [tx.amount for tx in result.transactions] == [
        Decimal("-19.55"),
        Decimal("-19.55"),
    ]
    assert [tx.payee for tx in result.transactions] == [
        "Insurance Example SA",
        "Insurance Example SA",
    ]
    assert [tx.import_id for tx in result.transactions] == [
        "INAB:BATCHREF.1",
        "INAB:BATCHREF.2",
    ]
    assert [tx.legacy_exact_import_ids for tx in result.transactions] == [
        ["INAB:DETAILREFONE"],
        ["INAB:DETAILREFTWO"],
    ]
    assert all(
        "IBAN: CH0000000000000000002" in (tx.memo or "") for tx in result.transactions
    )


def test_recurring_structured_references_do_not_duplicate_split_import_ids() -> None:
    def entry(entry_ref: str, booking_date: str) -> str:
        return f"""
<Ntry>
  <Amt Ccy="CHF">30.90</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>{booking_date}</Dt></BookgDt>
  <ValDt><Dt>{booking_date}</Dt></ValDt>
  <AcctSvcrRef>{entry_ref}</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">29.90</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Telecom Example SA</Nm></Pty></Cdtr><CdtrAcct><Id><IBAN>CH0000000000000000003</IBAN></Id></CdtrAcct></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>MONTHLYREF</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
    <TxDtls>
      <Amt Ccy="CHF">1.00</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdPties><Cdtr><Pty><Nm>Other Example SA</Nm></Pty></Cdtr><CdtrAcct><Id><IBAN>CH0000000000000000004</IBAN></Id></CdtrAcct></RltdPties>
      <RmtInf><Strd><CdtrRefInf><Ref>OTHER-{entry_ref}</Ref></CdtrRefInf></Strd></RmtInf>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Paiement groupé</AddtlNtryInf>
</Ntry>
"""

    content = camt_document(
        statement_xml(
            "CH111",
            entry("ENTRYREF1", "2026-04-28") + entry("ENTRYREF2", "2026-05-28"),
            opening="100.00",
            closing="38.20",
        )
    )

    result = parse_camt(content)

    telecom_transactions = [
        tx for tx in result.transactions if tx.payee == "Telecom Example SA"
    ]
    assert [tx.source_ref for tx in telecom_transactions] == [
        "ENTRYREF1.1",
        "ENTRYREF2.1",
    ]
    assert [tx.import_id for tx in telecom_transactions] == [
        "INAB:ENTRYREF1.1",
        "INAB:ENTRYREF2.1",
    ]
    assert all("Ref: MONTHLYREF" in (tx.memo or "") for tx in telecom_transactions)


def test_card_purchase_memo_is_compact_and_single_line() -> None:
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml(
                "10.00",
                "DBIT",
                "REF1",
                "Achat Coop-1959 Oron-la-\n30.03.2026, 14:45, No carte Visa Debit 400000xxxxxx0002",
            ),
            opening="100.00",
            closing="90.00",
        )
    )

    result = parse_camt(content)
    tx = result.transactions[0]

    assert tx.payee == "Coop"
    assert tx.memo == "Card: 30.03.2026 14:45"


def test_card_purchase_uses_payment_date_as_transaction_date() -> None:
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml(
                "34.30",
                "DBIT",
                "19973057037",
                "Achat Sample Bistro\n15.01.2026, 13:58, No carte Visa Debit 400000xxxxxx0002",
                booking_date="2026-01-19",
                value_date="2026-01-19",
            ),
            opening="100.00",
            closing="65.70",
        )
    )

    result = parse_camt(content)
    tx = result.transactions[0]

    assert tx.booking_date == date(2026, 1, 15)
    assert tx.value_date == date(2026, 1, 19)


def test_twint_payment_memo_is_compact_and_single_line() -> None:
    content = camt_document(
        statement_xml(
            "CH111",
            entry_xml(
                "5.80",
                "DBIT",
                "TWINTREF",
                "Achat TWINT SBB MOBILE\n30.03.2026, 14:45",
                booking_date="2026-03-31",
                value_date="2026-03-31",
            ),
            opening="100.00",
            closing="94.20",
        )
    )

    result = parse_camt(content)
    tx = result.transactions[0]

    assert tx.payee == "SBB Mobile"
    assert tx.memo == "TWINT: 30.03.2026 14:45"
    assert tx.booking_date == date(2026, 3, 30)
    assert tx.value_date == date(2026, 3, 31)


def test_twint_payment_without_timestamp_omits_bank_noise() -> None:
    entry = """
<Ntry>
  <Amt Ccy="CHF">119.00</Amt>
  <CdtDbtInd>DBIT</CdtDbtInd>
  <RvslInd>false</RvslInd>
  <Sts><Cd>BOOK</Cd></Sts>
  <BookgDt><Dt>2026-05-06</Dt></BookgDt>
  <ValDt><Dt>2026-05-06</Dt></ValDt>
  <AcctSvcrRef>TWINTREF</AcctSvcrRef>
  <NtryDtls>
    <TxDtls>
      <Amt Ccy="CHF">119.00</Amt>
      <CdtDbtInd>DBIT</CdtDbtInd>
      <RltdAgts>
        <CdtrAgt><FinInstnId><Nm>Raiffeisen Schweiz</Nm></FinInstnId></CdtrAgt>
      </RltdAgts>
    </TxDtls>
  </NtryDtls>
  <AddtlNtryInf>Achat TWINT DIGITEC GALAXUS</AddtlNtryInf>
</Ntry>
"""
    content = camt_document(
        statement_xml("CH111", entry, opening="500.00", closing="381.00")
    )

    result = parse_camt(content)
    tx = result.transactions[0]

    assert tx.payee == "Digitec Galaxus"
    assert tx.memo == "TWINT"
    assert tx.booking_date == date(2026, 5, 6)
