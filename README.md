# INAB

INAB is a self-hosted importer for Swiss bank CAMT.053 exports and a supported CSV bank export into YNAB.

The app is intentionally small: it runs a password-gated web UI, parses uploaded CAMT XML locally, previews transactions and likely internal transfers, then imports the confirmed rows through the official YNAB Python SDK.

## Features

- Drag/drop CAMT.053 XML uploads and supported semicolon CSV uploads.
- Multi-account CAMT files with one or more `<Stmt>` blocks.
- IBAN-to-YNAB account mapping stored in local SQLite.
- Duplicate prevention through deterministic YNAB `import_id` values.
- Preview of unambiguous checking/savings transfer pairs.
- YNAB token supplied only through `YNAB_ACCESS_TOKEN`; it is never stored in SQLite.

MT940 exports are rejected in v1. See [docs/format-choice.md](docs/format-choice.md) for the rationale.

## Configuration

Required environment variables:

```sh
export YNAB_ACCESS_TOKEN="..."
export INAB_USERNAME="inab"
export INAB_PASSWORD="choose-a-password"
```

Optional environment variables:

```sh
export INAB_DATA_DIR="./data"
export INAB_SESSION_SECRET="stable-cookie-signing-secret"
export INAB_MAX_UPLOAD_BYTES="10485760"
export INAB_TARGET_CURRENCY="CHF"
export INAB_SELF_NAMES="Alex Example,Example Alex"
export INAB_CSV_ACCOUNT_IBAN="CH..."
```

For self-hosting, put the app behind HTTPS and keep `INAB_DATA_DIR` on persistent storage.

## Run Locally

```sh
uv sync --extra dev
uv run uvicorn inab.web:create_app --factory --reload
```

Open `http://127.0.0.1:8000`, log in, select your YNAB plan, then upload a CAMT.053 XML export once so the app can discover IBANs. Map each discovered IBAN to a YNAB account in Setup, then upload again to preview and import. For CSV uploads, set the CSV account IBAN or account key in Setup first because the CSV file itself does not include one.

## Docker

```sh
docker build -t inab .
docker run --rm -p 8000:8000 \
  -e YNAB_ACCESS_TOKEN="$YNAB_ACCESS_TOKEN" \
  -e INAB_USERNAME="inab" \
  -e INAB_PASSWORD="choose-a-password" \
  -v "$PWD/data:/data" \
  inab
```

## Import Behavior

- CAMT `DBIT` entries become negative YNAB amounts; `CRDT` entries become positive amounts.
- CSV `Amount` values are already signed; the app imports positive values as inflows and negative values as outflows.
- Amounts are converted to YNAB milliunits with `Decimal * 1000`.
- Imported transactions are created with `cleared="cleared"` and `approved=false`.
- `AcctSvcrRef` becomes `INAB:<AcctSvcrRef>` when it fits YNAB’s 36-character import ID limit.
- Entries without a usable bank reference, including CSV rows, get a deterministic hash-based `INAB:<hash>` import ID.
- Before import, the app fetches existing YNAB transactions for each mapped account from the earliest uploaded date and skips matching import IDs.
- Accepted internal transfers are imported once from the debit-side account using the target account’s `transfer_payee_id`; the credit-side CAMT row is skipped as the transfer counterpart.
- If a CAMT entry contains richer `NtryDtls/TxDtls` data, generic labels such as `Ordre permanent` and `Paiement groupé` are enriched with counterparty and remittance details.
- Grouped CAMT entries are split into individual YNAB transactions only when every detail has an amount and the signed detail total exactly reconciles to the booked entry amount.
- Payees are normalized from bank labels when no better structured counterparty exists. For example, `Achat TWINT SBB MOBILE` becomes `SBB Mobile`, `Achat online ... No carte ...` drops card/date details, and `Paiement TWINT EXAMPLE, ALEX` becomes `Alex Example`.
- CAMT detail counterparties are recorded with name, IBAN, and bank when present. Setup can label known counterparty IBANs for your own external accounts.
- When a transaction's counterparty name matches an own-name alias and the counterparty IBAN has a saved label, INAB relabels it as `Transfer to <label>` or `Transfer from <label>` before preview/import. Own-name aliases can be supplied with `INAB_SELF_NAMES` or edited in Setup.
- Multi-account CAMT files may include empty statement blocks. INAB records those IBANs as observed accounts, but only IBANs with transactions must be mapped before import.
- CSV memo fields include the source description plus subject, category, tags, Wise/Spaces flags, and original FX amount when present.

## YNAB API Notes

INAB uses the official `ynab` package, currently pinned to `4.1.0`, generated from YNAB API spec `1.83.0`.

The app uses:

- `PlansApi.get_plans`
- `AccountsApi.get_accounts`
- `TransactionsApi.get_transactions_by_account`
- `TransactionsApi.create_transaction`

It does not use `TransactionsApi.import_transactions`; that endpoint triggers import for linked/direct-import accounts and does not upload custom parsed statement rows.

Primary references:

- <https://api.ynab.com/>
- <https://api.ynab.com/papi/open_api_spec.yaml>
- <https://github.com/ynab/ynab-sdk-python>
- <https://pypi.org/project/ynab/4.1.0/>

## Tests

```sh
uv run pytest
```

The parser test will use the local `sample/` CAMT export when present. The sample directory is ignored by git because bank exports contain private account data.
