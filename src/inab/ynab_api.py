from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol


class YnabError(RuntimeError):
    pass


@dataclass(frozen=True)
class YnabPlan:
    id: str
    name: str


@dataclass(frozen=True)
class YnabAccount:
    id: str
    name: str
    type: str | None
    closed: bool
    deleted: bool
    transfer_payee_id: str | None


@dataclass(frozen=True)
class YnabCategory:
    id: str
    name: str
    group_name: str
    hidden: bool
    deleted: bool


@dataclass(frozen=True)
class YnabPayee:
    id: str
    name: str
    transfer_account_id: str | None
    deleted: bool


@dataclass(frozen=True)
class CreateTransactionsResult:
    transaction_ids: list[str]
    duplicate_import_ids: list[str]


class YnabGateway(Protocol):
    def list_plans(self) -> list[YnabPlan]:
        ...

    def list_accounts(self, plan_id: str) -> list[YnabAccount]:
        ...

    def list_categories(self, plan_id: str) -> list[YnabCategory]:
        ...

    def list_payees(self, plan_id: str) -> list[YnabPayee]:
        ...

    def existing_import_ids(self, plan_id: str, account_id: str, since_date: date) -> set[str]:
        ...

    def create_transactions(self, plan_id: str, transactions: list[dict[str, Any]]) -> CreateTransactionsResult:
        ...


class OfficialYnabGateway:
    def __init__(self, access_token: str):
        if not access_token:
            raise YnabError("YNAB_ACCESS_TOKEN is not configured.")
        self.access_token = access_token

    def list_plans(self) -> list[YnabPlan]:
        ynab = _ynab()
        try:
            with ynab.ApiClient(ynab.Configuration(access_token=self.access_token)) as api_client:
                response = ynab.PlansApi(api_client).get_plans()
        except Exception as exc:
            raise YnabError(_safe_error("Could not list YNAB plans", exc)) from exc
        return [YnabPlan(id=str(plan.id), name=plan.name) for plan in response.data.plans]

    def list_accounts(self, plan_id: str) -> list[YnabAccount]:
        ynab = _ynab()
        try:
            with ynab.ApiClient(ynab.Configuration(access_token=self.access_token)) as api_client:
                response = ynab.AccountsApi(api_client).get_accounts(plan_id)
        except Exception as exc:
            raise YnabError(_safe_error("Could not list YNAB accounts", exc)) from exc
        return [
            YnabAccount(
                id=str(account.id),
                name=account.name,
                type=getattr(account, "type", None),
                closed=bool(getattr(account, "closed", False)),
                deleted=bool(getattr(account, "deleted", False)),
                transfer_payee_id=_optional_str(getattr(account, "transfer_payee_id", None)),
            )
            for account in response.data.accounts
            if not bool(getattr(account, "deleted", False))
        ]

    def list_categories(self, plan_id: str) -> list[YnabCategory]:
        ynab = _ynab()
        try:
            with ynab.ApiClient(ynab.Configuration(access_token=self.access_token)) as api_client:
                response = ynab.CategoriesApi(api_client).get_categories(plan_id)
        except Exception as exc:
            raise YnabError(_safe_error("Could not list YNAB categories", exc)) from exc
        categories: list[YnabCategory] = []
        for group in response.data.category_groups:
            group_deleted = bool(getattr(group, "deleted", False))
            group_hidden = bool(getattr(group, "hidden", False))
            group_name = str(getattr(group, "name", ""))
            for category in getattr(group, "categories", []) or []:
                categories.append(
                    YnabCategory(
                        id=str(category.id),
                        name=category.name,
                        group_name=group_name,
                        hidden=group_hidden or bool(getattr(category, "hidden", False)),
                        deleted=group_deleted or bool(getattr(category, "deleted", False)),
                    )
                )
        return categories

    def list_payees(self, plan_id: str) -> list[YnabPayee]:
        ynab = _ynab()
        try:
            with ynab.ApiClient(ynab.Configuration(access_token=self.access_token)) as api_client:
                response = ynab.PayeesApi(api_client).get_payees(plan_id)
        except Exception as exc:
            raise YnabError(_safe_error("Could not list YNAB payees", exc)) from exc
        return [
            YnabPayee(
                id=str(payee.id),
                name=payee.name,
                transfer_account_id=_optional_str(getattr(payee, "transfer_account_id", None)),
                deleted=bool(getattr(payee, "deleted", False)),
            )
            for payee in response.data.payees
        ]

    def existing_import_ids(self, plan_id: str, account_id: str, since_date: date) -> set[str]:
        ynab = _ynab()
        try:
            with ynab.ApiClient(ynab.Configuration(access_token=self.access_token)) as api_client:
                response = ynab.TransactionsApi(api_client).get_transactions_by_account(
                    plan_id,
                    account_id,
                    since_date=since_date,
                )
        except Exception as exc:
            raise YnabError(_safe_error("Could not fetch existing YNAB transactions", exc)) from exc
        return {
            transaction.import_id
            for transaction in response.data.transactions
            if getattr(transaction, "import_id", None)
        }

    def create_transactions(self, plan_id: str, transactions: list[dict[str, Any]]) -> CreateTransactionsResult:
        if not transactions:
            return CreateTransactionsResult(transaction_ids=[], duplicate_import_ids=[])
        ynab = _ynab()
        try:
            with ynab.ApiClient(ynab.Configuration(access_token=self.access_token)) as api_client:
                wrapper = ynab.PostTransactionsWrapper(transactions=transactions)
                response = ynab.TransactionsApi(api_client).create_transaction(plan_id, wrapper)
        except Exception as exc:
            raise YnabError(_safe_error("Could not create YNAB transactions", exc)) from exc
        data = response.data
        return CreateTransactionsResult(
            transaction_ids=list(getattr(data, "transaction_ids", []) or []),
            duplicate_import_ids=list(getattr(data, "duplicate_import_ids", []) or []),
        )


def _ynab() -> Any:
    try:
        import ynab
    except ImportError as exc:
        raise YnabError("The official `ynab` package is not installed.") from exc
    return ynab


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _safe_error(message: str, exc: Exception) -> str:
    status = getattr(exc, "status", None)
    reason = getattr(exc, "reason", None)
    if status and reason:
        return f"{message}: {status} {reason}"
    if status:
        return f"{message}: HTTP {status}"
    return f"{message}: {exc.__class__.__name__}"
