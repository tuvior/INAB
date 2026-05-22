from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from inab.actual_api import ActualBudgetGateway, ActualBudgetSettings
from inab.budget_api import ImportTransaction


class FakeActual:
    instances: list["FakeActual"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.session = FakeSession()
        self.committed = False
        FakeActual.instances.append(self)

    def __enter__(self) -> "FakeActual":
        return self

    def __exit__(self, *args: Any) -> None:
        self.session.closed = True
        return None

    def list_user_files(self) -> Any:
        return SimpleNamespace(
            data=[SimpleNamespace(file_id="budget-id", name="Household", deleted=0)]
        )

    def commit(self) -> None:
        self.committed = True


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.categories = {"cat-food": SimpleNamespace(id="cat-food", tombstone=0)}
        self.transactions = {"tx-1": SimpleNamespace(id="tx-1", tombstone=0)}

    def get(self, model: Any, item_id: str) -> Any:
        if model is FakeCategories:
            return self.categories.get(item_id)
        if model is FakeTransactions:
            return self.transactions.get(item_id)
        return None


class FakeCategories:
    pass


class FakeTransactions:
    pass


class FakeDatabase:
    Categories = FakeCategories
    Transactions = FakeTransactions


class FakeTransaction:
    def __init__(
        self,
        *,
        id: str,
        financial_id: str | None,
        amount: Decimal,
        transaction_date: date,
    ) -> None:
        self.id = id
        self.financial_id = financial_id
        self.acct = "checking-id"
        self.amount = amount
        self.transaction_date = transaction_date
        self.transferred_id = None
        self.tombstone = 0

    def get_date(self) -> date:
        return self.transaction_date

    def get_amount(self) -> Decimal:
        return self.amount

    def delete(self) -> None:
        self.tombstone = 1


class FakeQueries:
    def __init__(self) -> None:
        self.reconciled: list[dict[str, Any]] = []
        self.transfers: list[dict[str, Any]] = []
        self.existing = [
            FakeTransaction(
                id="existing-1",
                financial_id="INAB:REF1",
                amount=Decimal("-10"),
                transaction_date=date(2026, 4, 10),
            )
        ]

    def get_categories(
        self, session: FakeSession, *, include_deleted: bool = False
    ) -> list["FakeCategory"]:
        return [FakeCategory(session)]

    def get_payees(
        self, session: FakeSession, *, include_deleted: bool = False
    ) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                id="payee-coop",
                name="Coop",
                transfer_acct=None,
                tombstone=0,
            )
        ]

    def get_transactions(
        self,
        session: Any,
        *,
        start_date: date | None = None,
        account: str | None = None,
    ) -> list[FakeTransaction]:
        self.existing_call = {"start_date": start_date, "account": account}
        return self.existing

    def reconcile_transaction(
        self, session: Any, transaction_date: date, account_id: str, **kwargs: Any
    ) -> FakeTransaction:
        self.reconciled.append(
            {"date": transaction_date, "account_id": account_id, **kwargs}
        )
        return FakeTransaction(
            id="created-1",
            financial_id=kwargs["imported_id"],
            amount=kwargs["amount"],
            transaction_date=transaction_date,
        )

    def create_transfer(
        self,
        session: Any,
        transaction_date: date,
        source_account: str,
        dest_account: str,
        amount: Decimal,
        *,
        notes: str | None = None,
    ) -> tuple[FakeTransaction, FakeTransaction]:
        self.transfers.append(
            {
                "date": transaction_date,
                "source_account": source_account,
                "dest_account": dest_account,
                "amount": amount,
                "notes": notes,
            }
        )
        return (
            FakeTransaction(
                id="transfer-source",
                financial_id=None,
                amount=-amount,
                transaction_date=transaction_date,
            ),
            FakeTransaction(
                id="transfer-target",
                financial_id=None,
                amount=amount,
                transaction_date=transaction_date,
            ),
        )


class FakeCategory:
    id = "cat-food"
    name = "Food"
    hidden = 0
    tombstone = 0

    def __init__(self, session: FakeSession) -> None:
        self.session = session

    @property
    def group(self) -> SimpleNamespace:
        if self.session.closed:
            raise RuntimeError("category group was lazy-loaded after session close")
        return SimpleNamespace(name="Everyday", hidden=0, tombstone=0)


def test_actual_gateway_lists_budgets_and_uses_actualpy_cache_dir(
    tmp_path: Path,
) -> None:
    FakeActual.instances = []
    gateway = _gateway(tmp_path)

    budgets = gateway.list_budgets()

    assert budgets[0].id == "budget-id"
    assert FakeActual.instances[0].kwargs["data_dir"] == tmp_path / "actual-cache"
    assert FakeActual.instances[0].kwargs["file"] is None


def test_actual_duplicate_detection_uses_financial_id(
    monkeypatch: Any, tmp_path: Path
) -> None:
    queries = FakeQueries()
    monkeypatch.setattr("inab.actual_api._queries", lambda: queries)
    gateway = _gateway(tmp_path)

    existing = gateway.existing_transactions(
        "budget-id", "checking-id", date(2026, 4, 1)
    )

    assert existing[0].import_id == "INAB:REF1"
    assert existing[0].amount == -10000
    assert queries.existing_call == {
        "start_date": date(2026, 4, 1),
        "account": "checking-id",
    }


def test_actual_gateway_materializes_categories_before_session_closes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    queries = FakeQueries()
    monkeypatch.setattr("inab.actual_api._queries", lambda: queries)
    gateway = _gateway(tmp_path)

    categories = gateway.list_categories("budget-id")

    assert categories[0].id == "cat-food"
    assert categories[0].group_name == "Everyday"


def test_actual_gateway_reconciles_normal_transactions_and_commits(
    monkeypatch: Any, tmp_path: Path
) -> None:
    queries = FakeQueries()
    monkeypatch.setattr("inab.actual_api._queries", lambda: queries)
    monkeypatch.setattr("inab.actual_api._database", lambda: FakeDatabase)
    gateway = _gateway(tmp_path)

    result = gateway.create_transactions(
        "budget-id",
        [
            ImportTransaction(
                account_id="checking-id",
                date=date(2026, 4, 10),
                amount=Decimal("-10.00"),
                amount_milliunits=-10000,
                payee_name="Coop",
                memo="Memo",
                import_id="INAB:REF1",
                category_id="cat-food",
            )
        ],
    )

    assert result.transaction_ids == ["created-1"]
    assert queries.reconciled[0]["category"].id == "cat-food"
    assert queries.reconciled[0]["imported_id"] == "INAB:REF1"
    assert FakeActual.instances[-1].committed is True


def test_actual_gateway_creates_transfer_and_sets_both_import_ids(
    monkeypatch: Any, tmp_path: Path
) -> None:
    queries = FakeQueries()
    monkeypatch.setattr("inab.actual_api._queries", lambda: queries)
    monkeypatch.setattr("inab.actual_api._database", lambda: FakeDatabase)
    gateway = _gateway(tmp_path)

    result = gateway.create_transactions(
        "budget-id",
        [
            ImportTransaction(
                account_id="checking-id",
                date=date(2026, 4, 10),
                amount=Decimal("-250.00"),
                amount_milliunits=-250000,
                payee_name="Transfer to savings",
                memo="Memo",
                import_id="INAB:DEBIT",
                transfer_account_id="savings-id",
                transfer_counterpart_import_id="INAB:CREDIT",
            )
        ],
    )

    assert result.transaction_ids == ["transfer-source", "transfer-target"]
    assert result.transactions[0]["import_id"] == "INAB:DEBIT"
    assert result.transactions[1]["import_id"] == "INAB:CREDIT"
    assert queries.transfers[0]["dest_account"] == "savings-id"
    assert FakeActual.instances[-1].committed is True


def test_actual_gateway_patches_and_rolls_back_category_notes(
    monkeypatch: Any, tmp_path: Path
) -> None:
    category = SimpleNamespace(id="cat-food", tombstone=0, notes="Existing note")

    class NoteSession(FakeSession):
        def get(self, model: Any, item_id: str) -> Any:
            if model is FakeCategories and item_id == "cat-food":
                return category
            return None

    class NoteActual(FakeActual):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.session = NoteSession()

    monkeypatch.setattr("inab.actual_api._database", lambda: FakeDatabase)
    gateway = ActualBudgetGateway(
        ActualBudgetSettings(
            base_url="https://actual.example",
            password="secret",
            data_dir=tmp_path / "actual-cache",
        ),
        actual_cls=NoteActual,
    )

    report = gateway.append_category_note_blocks(
        "budget-id",
        [
            {
                "category_id": "cat-food",
                "category_name": "Everyday: Food",
                "source_category_id": "ynab-food",
                "migration_id": "mig-1",
                "block": "#template 25.00",
            }
        ],
    )

    assert report[0]["patched"] is True
    assert category.notes == "Existing note\n\n#template 25.00"
    assert report[0]["before"] == "Existing note"
    assert report[0]["after"] == "Existing note\n\n#template 25.00"
    assert FakeActual.instances[-1].committed is True

    rollback = gateway.rollback_category_note_blocks("budget-id", report)

    assert rollback[0]["rolled_back"] is True
    assert category.notes == "Existing note"
    assert FakeActual.instances[-1].committed is True


def _gateway(tmp_path: Path) -> ActualBudgetGateway:
    return ActualBudgetGateway(
        ActualBudgetSettings(
            base_url="https://actual.example",
            password="secret",
            data_dir=tmp_path / "actual-cache",
        ),
        actual_cls=FakeActual,
    )
