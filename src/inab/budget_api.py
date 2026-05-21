from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol


class BudgetError(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetRef:
    id: str
    name: str


@dataclass(frozen=True)
class BudgetAccount:
    id: str
    name: str
    type: str | None
    closed: bool
    deleted: bool
    transfer_payee_id: str | None = None


@dataclass(frozen=True)
class BudgetCategory:
    id: str
    name: str
    group_name: str
    hidden: bool
    deleted: bool


@dataclass(frozen=True)
class BudgetPayee:
    id: str
    name: str
    transfer_account_id: str | None
    deleted: bool


@dataclass(frozen=True)
class ExistingTransaction:
    import_id: str
    date: date | None
    amount: int | None


@dataclass(frozen=True)
class ImportTransaction:
    account_id: str
    date: date
    amount: Decimal
    amount_milliunits: int
    payee_name: str
    memo: str | None
    import_id: str
    category_id: str | None = None
    cleared: bool = True
    transfer_account_id: str | None = None
    transfer_payee_id: str | None = None
    transfer_counterpart_import_id: str | None = None


@dataclass(frozen=True)
class CreateTransactionsResult:
    transaction_ids: list[str]
    duplicate_import_ids: list[str]
    transactions: list[dict[str, Any]] = field(default_factory=list)


class BudgetGateway(Protocol):
    backend_name: str
    backend_label: str

    def list_budgets(self) -> list[BudgetRef]: ...

    def list_accounts(self, budget_id: str) -> list[BudgetAccount]: ...

    def list_categories(self, budget_id: str) -> list[BudgetCategory]: ...

    def list_payees(self, budget_id: str) -> list[BudgetPayee]: ...

    def existing_transactions(
        self, budget_id: str, account_id: str, since_date: date | None = None
    ) -> list[ExistingTransaction]: ...

    def create_transactions(
        self, budget_id: str, transactions: list[ImportTransaction]
    ) -> CreateTransactionsResult: ...

    def delete_transaction(self, budget_id: str, transaction_id: str) -> None: ...
