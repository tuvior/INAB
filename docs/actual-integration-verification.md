# Actual Budget Integration Verification

Use this checklist against a disposable Actual Budget file before relying on Actual support or the YNAB-to-Actual migration wizard in production.

## Prerequisites

- A running Actual Budget server.
- A disposable Actual budget with at least two active accounts.
- INAB env vars configured:

```sh
export ACTUAL_BASE_URL="https://actual.example"
export ACTUAL_PASSWORD="..."
export ACTUAL_DATA_DIR="./data/actual-cache"
```

Choose the disposable budget and accounts for verification:

```sh
export INAB_VERIFY_BUDGET_ID="disposable-budget-file-id-or-name"
export INAB_VERIFY_ACCOUNT_ID="actual-account-id"
export INAB_VERIFY_TRANSFER_ACCOUNT_ID="other-actual-account-id"
export INAB_VERIFY_CATEGORY_ID="optional-category-id"
```

## Read-only Check

```sh
uv run python scripts/verify_actual_integration.py
```

This lists budgets, accounts, categories, and recent imported IDs without writing to Actual.

## Disposable Write Check

```sh
uv run python scripts/verify_actual_integration.py --apply
```

With `--apply`, the script:

- creates a small test transaction and then deletes it through INAB's Actual gateway;
- creates a small transfer pair and then deletes both sides;
- appends an INAB-marked category-note block when `INAB_VERIFY_CATEGORY_ID` is set;
- rolls back the note block;
- reports each created and deleted Actual transaction ID.

Only run this against a disposable budget. actualpy commits are not atomic if interrupted, so inspect the Actual UI afterward and delete any leftover verification rows manually if the script is stopped midway.
