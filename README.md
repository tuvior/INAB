# INAB

Import Swiss bank statements into YNAB from a self-hosted web app.

INAB parses CAMT.053 XML and supported CSV exports locally, lets you review them, then creates the confirmed transactions in YNAB.

![INAB import preview](docs/assets/inab-preview.png)

## How It Works

![INAB import flow](docs/assets/inab-flow.svg)

1. Export a CAMT.053 XML file or supported CSV file from your bank.
2. Upload it to INAB. Parsing, rules, duplicate checks, and transfer detection run locally.
3. Review the preview screen.
4. Import approved rows to YNAB through `TransactionsApi.create_transaction`.

## What It Does

- Imports Swiss CAMT.053 account statements and supported semicolon CSV exports.
- Handles multi-account CAMT files with one or more `<Stmt>` blocks.
- Stores IBAN-to-YNAB account mappings in local SQLite.
- Previews rows before import, including reconciliation totals and duplicate matches.
- Detects likely internal transfers between mapped accounts.
- Uses deterministic YNAB `import_id` values to avoid repeat imports.
- Keeps import history and can undo transactions created by INAB.

## Supported Banks And Formats

| Format       | Support                                                                   |
| ------------ | ------------------------------------------------------------------------- |
| CAMT.053 XML | Tested Swiss CAMT.053 exports are supported.                              |
| CSV          | Neon CSV exports are supported. Other bank CSV layouts are not supported yet. |
| MT940        | Not supported                                                             |

## Quick Start

```sh
uv sync --extra dev

export YNAB_ACCESS_TOKEN="..."
export INAB_USERNAME="inab"
export INAB_PASSWORD="choose-a-password"

uv run uvicorn inab.web:create_app --factory --reload
```

Open <http://127.0.0.1:8000> and sign in. Choose your YNAB plan in Setup, then upload a CAMT.053 XML file once so INAB can discover IBANs. Map each discovered IBAN to a YNAB account, upload again, review the preview, and import.

For CSV uploads, choose the target YNAB account on the upload form. The supported CSV format does not include an account identifier.

## Privacy And Data

INAB runs on your machine or server. Bank exports are parsed on the machine running the app, and local state is stored in SQLite under `INAB_DATA_DIR`.

Stored locally:

- account mappings
- import rules
- import history
- observed IBANs and counterparty labels
- YNAB transaction IDs created by INAB, used for undo

Sent to YNAB:

- transactions you confirm for import
- account, plan, payee, and category lookups needed by the UI

Not stored in SQLite:

- `YNAB_ACCESS_TOKEN`, which stays in your deployment environment
- the uploaded bank file itself

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

If you expose INAB outside localhost, put it behind HTTPS. Keep `/data` on persistent storage.

## Configuration

Required:

```sh
YNAB_ACCESS_TOKEN="..."
INAB_USERNAME="inab"
INAB_PASSWORD="choose-a-password"
```

Optional:

```sh
INAB_DATA_DIR="./data"
INAB_SESSION_SECRET="stable-cookie-signing-secret"
INAB_MAX_UPLOAD_BYTES="10485760"
INAB_TARGET_CURRENCY="CHF"
INAB_SELF_NAMES="Alex Example,Example Alex"
INAB_ROOT_PATH="/inab"
```

Set `INAB_ROOT_PATH` only when publishing the app under a URL prefix such as `https://example.test/inab`.

## Import Rules

INAB can rewrite payees and assign YNAB categories before import. Rules are edited in the web UI and stored in SQLite. The preview shows the original payee, matched rule, and assigned category before import.

## Import Behavior

- CAMT `DBIT` entries become negative YNAB amounts; `CRDT` entries become positive amounts.
- CSV `Amount` values are already signed.
- Amounts are converted to YNAB milliunits with `Decimal * 1000`.
- Imported transactions are created with `cleared="cleared"` and `approved=false`.
- `AcctSvcrRef` becomes `INAB:<AcctSvcrRef>` when it fits YNAB's 36-character import ID limit.
- Rows without a usable bank reference get a deterministic hash-based `INAB:<hash>` import ID.
- Before import, INAB fetches existing YNAB transactions for each mapped account and skips matching import IDs.
- YNAB can match an INAB-imported row to a user-entered transaction on the same account with the same amount and a date within 10 days.
- Transfer pairs are imported once from the debit-side account using the target account's `transfer_payee_id`.
- Preview, blocked, and failed jobs without created YNAB transaction IDs are pruned after 7 days.

## YNAB API

INAB uses the official `ynab` package, pinned to `4.1.0` and generated from YNAB API spec `1.83.0`.

Used endpoints:

- `PlansApi.get_plans`
- `AccountsApi.get_accounts`
- `TransactionsApi.get_transactions_by_account`
- `TransactionsApi.create_transaction`
- `TransactionsApi.delete_transaction`

INAB does not use `TransactionsApi.import_transactions`; that endpoint triggers YNAB import for linked/direct-import accounts and does not upload parsed statement rows.

References:

- <https://api.ynab.com/>
- <https://api.ynab.com/papi/open_api_spec.yaml>
- <https://github.com/ynab/ynab-sdk-python>
- <https://pypi.org/project/ynab/4.1.0/>

## Tests

```sh
uv run pytest
```

Parser tests use the local `sample/` CAMT export when present. The sample directory is ignored by git because bank exports contain private account data.
