from __future__ import annotations

import argparse
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

from inab.actual_api import ActualBudgetGateway, ActualBudgetSettings
from inab.budget_api import ImportTransaction


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify INAB Actual Budget integration against a disposable budget."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create and undo disposable transactions and category note patches.",
    )
    args = parser.parse_args()

    gateway = ActualBudgetGateway(
        ActualBudgetSettings(
            base_url=_required("ACTUAL_BASE_URL"),
            password=_required("ACTUAL_PASSWORD"),
            encryption_password=os.environ.get("ACTUAL_ENCRYPTION_PASSWORD") or None,
            data_dir=(
                Path(os.environ["ACTUAL_DATA_DIR"]).expanduser()
                if os.environ.get("ACTUAL_DATA_DIR")
                else None
            ),
            verify_ssl=_verify_ssl(os.environ.get("ACTUAL_VERIFY_SSL")),
        )
    )
    budget_id = _required("INAB_VERIFY_BUDGET_ID")

    budgets = gateway.list_budgets()
    print(f"budgets: {len(budgets)}")
    accounts = gateway.list_accounts(budget_id)
    categories = gateway.list_categories(budget_id)
    print(f"accounts: {len(accounts)}")
    print(f"categories: {len(categories)}")

    account_id = os.environ.get("INAB_VERIFY_ACCOUNT_ID")
    transfer_account_id = os.environ.get("INAB_VERIFY_TRANSFER_ACCOUNT_ID")
    category_id = os.environ.get("INAB_VERIFY_CATEGORY_ID")
    if account_id:
        existing = gateway.existing_transactions(
            budget_id, account_id, since_date=date.today()
        )
        print(f"existing imported ids today on verification account: {len(existing)}")

    if not args.apply:
        print("read-only verification complete; pass --apply for disposable writes")
        return
    if not account_id:
        raise SystemExit("INAB_VERIFY_ACCOUNT_ID is required with --apply.")

    normal = gateway.create_transactions(
        budget_id,
        [
            ImportTransaction(
                account_id=account_id,
                date=date.today(),
                amount=Decimal("-0.01"),
                amount_milliunits=-10,
                payee_name="INAB Actual verification",
                memo="Created by scripts/verify_actual_integration.py",
                import_id="INAB:VERIFY-NORMAL",
                category_id=category_id,
            )
        ],
    )
    print(f"created normal transaction ids: {normal.transaction_ids}")
    for transaction_id in normal.transaction_ids:
        gateway.delete_transaction(budget_id, transaction_id)
    print("deleted normal verification transaction")

    if transfer_account_id:
        transfer = gateway.create_transactions(
            budget_id,
            [
                ImportTransaction(
                    account_id=account_id,
                    date=date.today(),
                    amount=Decimal("-0.02"),
                    amount_milliunits=-20,
                    payee_name="INAB Actual transfer verification",
                    memo="Created by scripts/verify_actual_integration.py",
                    import_id="INAB:VERIFY-TRANSFER-SOURCE",
                    transfer_account_id=transfer_account_id,
                    transfer_counterpart_import_id="INAB:VERIFY-TRANSFER-TARGET",
                )
            ],
        )
        print(f"created transfer transaction ids: {transfer.transaction_ids}")
        for transaction_id in transfer.transaction_ids:
            gateway.delete_transaction(budget_id, transaction_id)
        print("deleted transfer verification transactions")

    if category_id:
        patch_report = gateway.append_category_note_blocks(
            budget_id,
            [
                {
                    "category_id": category_id,
                    "category_name": category_id,
                    "source_category_id": "verification",
                    "migration_id": "verification",
                    "block": "#template 0.01",
                }
            ],
        )
        print(f"note patch report: {patch_report}")
        rollback_report = gateway.rollback_category_note_blocks(budget_id, patch_report)
        print(f"note rollback report: {rollback_report}")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required.")
    return value


def _verify_ssl(value: str | None) -> bool | str:
    if value is None or not value.strip():
        return True
    normalized = value.strip().casefold()
    if normalized in {"0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    return value.strip()


if __name__ == "__main__":
    main()
