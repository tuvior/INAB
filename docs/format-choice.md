# Format Choice

INAB v1 supports CAMT.053 XML and one semicolon-delimited CSV export. MT940 uploads are rejected with a clear message.

## Samples Analyzed

The local `sample/` directory contains:

- `camt053_001_08_ch0000000000000000000_20260519071007.xml`
- `Konto_CH0000000000000000000_20260519071045.csv`
- `Konto_CH0000000000000000000_20260519071028.mt940`
- `2026_4_account_statements19052026114500.csv`
- `2026_4_account_statements19052026114515.csv`

The sample directory is intentionally gitignored because the files contain private banking data.

## CAMT.053 XML

CAMT.053 is the best v1 source format.

Observed strengths:

- UTF-8 XML with explicit ISO 20022 structure.
- One or more `<Stmt>` blocks, so multi-account exports can be handled naturally.
- Account IBAN, account currency, owner, bank name, statement period, opening balance, and closing balance are explicit.
- The sample has 90 booked entries and every entry has a unique `AcctSvcrRef`.
- The sample opening balance plus signed movement total reconciles to the closing balance.
- Multiline transaction descriptions survive without CSV quoting or encoding ambiguity.
- Updated samples include detailed `NtryDtls/TxDtls` blocks for generic entries such as `Ordre permanent`, including counterparty name, IBAN, bank agent, remittance text, and structured references.

This gives INAB enough information to group by bank account, validate balances, create stable YNAB import IDs, and block unknown IBANs before import.

## Raiffeisen CSV

The original Raiffeisen CSV export is easy to inspect but weaker for safe imports.

Observed fields:

- `IBAN`
- `Booked At`
- `Text`
- `Credit/Debit Amount`
- `Balance`
- `Valuta Date`

The Raiffeisen CSV sample has 90 April rows and the same CHF 740.37 total movement as the CAMT sample. Its main drawback is that it has no stable per-transaction bank reference. Duplicate prevention would have to rely on date, amount, payee text, and occurrence counting, which is less robust than CAMT `AcctSvcrRef`. The file is also ISO-8859 text, which adds encoding risk.

## Other-Bank CSV

The newer CSV export has these columns:

- `Date`
- `Amount`
- `Original amount`
- `Original currency`
- `Exchange rate`
- `Description`
- `Subject`
- `Category`
- `Tags`
- `Wise`
- `Spaces`

This CSV is supported because it is the only available export for that bank. The account identifier is not present in the file, so INAB asks for the target YNAB account on each CSV upload. The selected account is used as a temporary account key for preview, duplicate detection, and deterministic import IDs. The `Amount` column is treated as the signed account-currency amount. CSV memos retain the description, subject when present, and original amount with currency for FX card transactions. Optional `no` values are ignored.

The April CSV sample has 24 transactions, covers 2026-04-03 through 2026-04-30, and includes the expected CHF 600.00 inflow from the Raiffeisen outflow visible in the CAMT sample.

## MT940

MT940 is a banking statement format, but the sample is less useful for this importer.

Observed traits:

- The sample is a different May export with 31 entries, not the same April period as the CSV/CAMT samples.
- It includes opening and closing balances.
- Transaction references are compact and use `NONREF`, so they do not give the same stable import ID foundation as CAMT.
- Descriptions are stored in tagged text blocks and are more brittle to parse correctly.

## Decision

Prefer CAMT.053 XML when the bank offers it.

Use the supported CSV format for the bank that only exposes CSV. It uses deterministic hash-based import IDs derived from the selected YNAB account, date, amount, payee, memo, and occurrence count. MT940 should only be added if a concrete need appears, because CAMT carries richer structured data for the same bank export workflow.
