from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .budget_api import (
    BudgetAccount,
    BudgetCategory,
    BudgetError,
    BudgetPayee,
    BudgetRef,
    CreateTransactionsResult,
    ExistingTransaction,
    ImportTransaction,
)


class ActualBudgetError(BudgetError):
    pass


@dataclass(frozen=True)
class ActualBudgetSettings:
    base_url: str
    password: str
    budget: str
    encryption_password: str | None = None
    data_dir: Path | None = None
    verify_ssl: bool | str = True


class ActualBudgetGateway:
    backend_name = "actual"
    backend_label = "Actual Budget"

    def __init__(
        self, settings: ActualBudgetSettings, *, actual_cls: Any | None = None
    ):
        if not settings.base_url:
            raise ActualBudgetError("ACTUAL_BASE_URL is not configured.")
        if not settings.password:
            raise ActualBudgetError("ACTUAL_PASSWORD is not configured.")
        if not settings.budget:
            raise ActualBudgetError("ACTUAL_BUDGET is not configured.")
        self.settings = settings
        self._actual_cls = actual_cls

    def list_budgets(self) -> list[BudgetRef]:
        try:
            with self._actual(file=None) as actual:
                files = actual.list_user_files().data
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not list Actual Budget files", exc)
            ) from exc
        return [
            BudgetRef(id=str(file.file_id), name=str(file.name))
            for file in files
            if not bool(getattr(file, "deleted", False))
        ]

    def list_accounts(self, budget_id: str) -> list[BudgetAccount]:
        try:
            with self._actual(file=budget_id) as actual:
                queries = _queries()
                accounts = queries.get_accounts(actual.session, include_deleted=True)
                payees = queries.get_payees(actual.session, include_deleted=True)
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not list Actual Budget accounts", exc)
            ) from exc
        transfer_payees = {
            str(payee.transfer_acct): str(payee.id)
            for payee in payees
            if getattr(payee, "transfer_acct", None) and not _deleted(payee)
        }
        return [
            BudgetAccount(
                id=str(account.id),
                name=str(account.name),
                type=_optional_str(getattr(account, "type", None)),
                closed=bool(getattr(account, "closed", False)),
                deleted=_deleted(account),
                transfer_payee_id=transfer_payees.get(str(account.id)),
            )
            for account in accounts
        ]

    def list_categories(self, budget_id: str) -> list[BudgetCategory]:
        try:
            with self._actual(file=budget_id) as actual:
                categories = _queries().get_categories(
                    actual.session, include_deleted=True
                )
                return [
                    _budget_category_from_actual(category) for category in categories
                ]
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not list Actual Budget categories", exc)
            ) from exc

    def list_payees(self, budget_id: str) -> list[BudgetPayee]:
        try:
            with self._actual(file=budget_id) as actual:
                payees = _queries().get_payees(actual.session, include_deleted=True)
                return [_budget_payee_from_actual(payee) for payee in payees]
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not list Actual Budget payees", exc)
            ) from exc

    def existing_transactions(
        self, budget_id: str, account_id: str, since_date: date | None = None
    ) -> list[ExistingTransaction]:
        try:
            with self._actual(file=budget_id) as actual:
                transactions = _queries().get_transactions(
                    actual.session, start_date=since_date, account=account_id
                )
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not fetch existing Actual Budget transactions", exc)
            ) from exc
        return [
            ExistingTransaction(
                import_id=str(transaction.financial_id),
                date=_transaction_date(transaction),
                amount=_actual_amount_to_milliunits(transaction),
            )
            for transaction in transactions
            if getattr(transaction, "financial_id", None)
        ]

    def create_transactions(
        self, budget_id: str, transactions: list[ImportTransaction]
    ) -> CreateTransactionsResult:
        if not transactions:
            return CreateTransactionsResult(transaction_ids=[], duplicate_import_ids=[])
        saved: list[Any] = []
        already_matched: list[Any] = []
        try:
            with self._actual(file=budget_id) as actual:
                queries = _queries()
                database = _database()
                for transaction in transactions:
                    category = _category_by_id(
                        actual.session, database, transaction.category_id
                    )
                    if transaction.transfer_account_id:
                        source, target = queries.create_transfer(
                            actual.session,
                            transaction.date,
                            transaction.account_id,
                            transaction.transfer_account_id,
                            abs(transaction.amount),
                            notes=transaction.memo,
                        )
                        source.financial_id = transaction.import_id
                        target.financial_id = transaction.transfer_counterpart_import_id
                        saved.extend([source, target])
                        continue
                    saved_transaction = queries.reconcile_transaction(
                        actual.session,
                        transaction.date,
                        transaction.account_id,
                        payee=transaction.payee_name,
                        notes=transaction.memo,
                        category=category,
                        amount=transaction.amount,
                        imported_id=transaction.import_id,
                        cleared=transaction.cleared,
                        imported_payee=transaction.payee_name,
                        already_matched=already_matched,
                    )
                    saved.append(saved_transaction)
                    already_matched.append(saved_transaction)
                actual.commit()
                summaries = [
                    _saved_transaction_summary(transaction) for transaction in saved
                ]
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not create Actual Budget transactions", exc)
            ) from exc
        return CreateTransactionsResult(
            transaction_ids=[
                summary["id"] for summary in summaries if summary.get("id")
            ],
            duplicate_import_ids=[],
            transactions=summaries,
        )

    def delete_transaction(self, budget_id: str, transaction_id: str) -> None:
        try:
            with self._actual(file=budget_id) as actual:
                transaction = actual.session.get(
                    _database().Transactions, transaction_id
                )
                if transaction is None:
                    raise ActualBudgetError(
                        f"Actual Budget transaction {transaction_id} was not found."
                    )
                if hasattr(transaction, "delete"):
                    transaction.delete()
                else:
                    transaction.tombstone = 1
                actual.commit()
        except ActualBudgetError:
            raise
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not delete Actual Budget transaction", exc)
            ) from exc

    @contextmanager
    def _actual(self, *, file: str | None) -> Iterator[Any]:
        actual_cls = self._actual_cls or _actual_cls()
        with actual_cls(
            base_url=self.settings.base_url,
            password=self.settings.password,
            file=file or None,
            encryption_password=self.settings.encryption_password,
            data_dir=self.settings.data_dir,
            cert=self.settings.verify_ssl,
        ) as actual:
            yield actual


def _actual_cls() -> Any:
    try:
        from actual import Actual
    except ImportError as exc:
        raise ActualBudgetError("The `actualpy` package is not installed.") from exc
    return Actual


def _queries() -> Any:
    try:
        from actual import queries
    except ImportError as exc:
        raise ActualBudgetError("The `actualpy` package is not installed.") from exc
    return queries


def _database() -> Any:
    try:
        from actual import database
    except ImportError as exc:
        raise ActualBudgetError("The `actualpy` package is not installed.") from exc
    return database


def _category_by_id(session: Any, database: Any, category_id: str | None) -> Any | None:
    if not category_id:
        return None
    category = session.get(database.Categories, category_id)
    if category is None or _deleted(category):
        raise ActualBudgetError(f"Actual Budget category {category_id} was not found.")
    return category


def _budget_category_from_actual(category: Any) -> BudgetCategory:
    group = getattr(category, "group", None)
    return BudgetCategory(
        id=str(category.id),
        name=str(category.name),
        group_name=str(getattr(group, "name", "") or ""),
        hidden=bool(getattr(category, "hidden", False))
        or bool(getattr(group, "hidden", False)),
        deleted=_deleted(category) or _deleted(group),
    )


def _budget_payee_from_actual(payee: Any) -> BudgetPayee:
    return BudgetPayee(
        id=str(payee.id),
        name=str(payee.name or _transfer_payee_name(payee)),
        transfer_account_id=_optional_str(getattr(payee, "transfer_acct", None)),
        deleted=_deleted(payee),
    )


def _transaction_date(transaction: Any) -> date | None:
    try:
        return transaction.get_date()
    except Exception:
        return None


def _actual_amount_to_milliunits(transaction: Any) -> int | None:
    try:
        return int((transaction.get_amount() * Decimal("1000")).quantize(Decimal("1")))
    except Exception:
        return None


def _saved_transaction_summary(transaction: Any) -> dict[str, Any]:
    transaction_date = _transaction_date(transaction)
    amount = None
    try:
        amount = str(transaction.get_amount().quantize(Decimal("0.001")))
    except Exception:
        pass
    return {
        "id": _optional_str(getattr(transaction, "id", None)),
        "date": transaction_date.isoformat() if transaction_date else None,
        "amount": amount,
        "account_id": _optional_str(getattr(transaction, "acct", None)),
        "account_name": _optional_str(
            getattr(getattr(transaction, "account", None), "name", None)
        ),
        "payee_name": _optional_str(
            getattr(getattr(transaction, "payee", None), "name", None)
        ),
        "category_name": _optional_str(
            getattr(getattr(transaction, "category", None), "name", None)
        ),
        "import_id": _optional_str(getattr(transaction, "financial_id", None)),
        "matched_transaction_id": None,
        "transfer_transaction_id": _optional_str(
            getattr(transaction, "transferred_id", None)
        ),
    }


def _transfer_payee_name(payee: Any) -> str:
    account = getattr(payee, "account", None)
    name = getattr(account, "name", None)
    return f"Transfer : {name}" if name else "Transfer"


def _deleted(value: Any) -> bool:
    return bool(getattr(value, "tombstone", False))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _safe_error(message: str, exc: Exception) -> str:
    return f"{message}: {exc.__class__.__name__}"
