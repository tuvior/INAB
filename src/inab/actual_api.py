from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
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

    def append_category_note_blocks(
        self, budget_id: str, patches: list[dict[str, str]]
    ) -> list[dict[str, str | bool | None]]:
        report: list[dict[str, str | bool | None]] = []
        if not patches:
            return report
        try:
            with self._actual(file=budget_id) as actual:
                database = _database()
                for patch in patches:
                    category_id = patch["category_id"]
                    block = patch["block"]
                    category = actual.session.get(database.Categories, category_id)
                    if category is None or _deleted(category):
                        report.append(
                            {
                                "category_id": category_id,
                                "category_name": patch.get("category_name"),
                                "patched": False,
                                "error": "Category not found.",
                            }
                        )
                        continue
                    existing = getattr(category, "notes", None) or ""
                    if _note_block_exists(existing, block):
                        report.append(
                            {
                                "category_id": category_id,
                                "category_name": patch.get("category_name"),
                                "patched": False,
                                "error": "Template line already exists.",
                            }
                        )
                        continue
                    category.notes = _append_note_block(existing, block)
                    report.append(
                        {
                            "category_id": category_id,
                            "category_name": patch.get("category_name"),
                            "source_category_id": patch.get("source_category_id"),
                            "migration_id": patch.get("migration_id"),
                            "block": block,
                            "patched": True,
                            "before": existing,
                            "after": category.notes,
                            "error": None,
                        }
                    )
                actual.commit()
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not patch Actual Budget category notes", exc)
            ) from exc
        return report

    def rollback_category_note_blocks(
        self, budget_id: str, patch_report: list[dict[str, Any]]
    ) -> list[dict[str, str | bool | None]]:
        report: list[dict[str, str | bool | None]] = []
        if not patch_report:
            return report
        try:
            with self._actual(file=budget_id) as actual:
                database = _database()
                for patch in patch_report:
                    if not patch.get("patched"):
                        continue
                    category_id = str(patch.get("category_id") or "")
                    before = str(patch.get("before") or "")
                    after = str(patch.get("after") or "")
                    block = str(patch.get("block") or "")
                    category = actual.session.get(database.Categories, category_id)
                    if category is None or _deleted(category):
                        report.append(
                            {
                                "category_id": category_id,
                                "rolled_back": False,
                                "error": "Category not found.",
                            }
                        )
                        continue
                    existing = getattr(category, "notes", None) or ""
                    updated = _rollback_note_block(
                        existing, before=before, after=after, block=block
                    )
                    if updated == existing:
                        report.append(
                            {
                                "category_id": category_id,
                                "rolled_back": False,
                                "error": "Current note no longer matches the patch report.",
                            }
                        )
                        continue
                    category.notes = updated
                    report.append(
                        {
                            "category_id": category_id,
                            "rolled_back": True,
                            "before": existing,
                            "after": updated,
                            "error": None,
                        }
                    )
                actual.commit()
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not roll back Actual Budget category notes", exc)
            ) from exc
        return report

    def migrate_ynab_flag_tags(
        self, budget_id: str, flags: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        if not flags:
            return report
        try:
            with self._actual(file=budget_id) as actual:
                queries = _queries()
                transactions = _all_transactions(actual.session, queries)
                tags_by_name = {
                    str(tag.tag): tag
                    for tag in queries.get_tags(actual.session, include_deleted=True)
                    if getattr(tag, "tag", None)
                }
                for flag in flags:
                    color = str(flag.get("color") or "").strip().lower()
                    target_name = str(flag.get("tag") or "").strip().lstrip("#")
                    if not color or not target_name:
                        continue
                    tag = _upsert_flag_tag(
                        actual.session,
                        queries,
                        tags_by_name,
                        color=color,
                        source_name=str(flag.get("name") or ""),
                        target_name=target_name,
                        color_hex=str(flag.get("color_hex") or "#690cb0"),
                        description=str(
                            flag.get("description") or "Imported from YNAB"
                        ),
                    )
                    transaction_result = _assign_flag_tag_to_transactions(
                        transactions,
                        expected_count=int(flag.get("transaction_count") or 0),
                        source_tag=color,
                        target_tag=target_name,
                    )
                    report.append(
                        {
                            "color": color,
                            "name": flag.get("name"),
                            "tag": target_name,
                            "tag_id": _optional_str(getattr(tag, "id", None)),
                            **transaction_result,
                        }
                    )
                actual.commit()
        except Exception as exc:
            raise ActualBudgetError(
                _safe_error("Could not migrate Actual Budget YNAB flag tags", exc)
            ) from exc
        return report

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


def _upsert_flag_tag(
    session: Any,
    queries: Any,
    tags_by_name: dict[str, Any],
    *,
    color: str,
    source_name: str,
    target_name: str,
    color_hex: str,
    description: str,
) -> Any:
    source_tag = _source_flag_tag(tags_by_name, color=color, source_name=source_name)
    target_tag = tags_by_name.get(target_name)
    if (
        source_tag is not None
        and target_tag is None
        and _looks_like_ynab_imported_tag(source_tag)
    ):
        _remove_tag_lookup(tags_by_name, source_tag)
        source_tag.tag = target_name
        source_tag.color = color_hex
        source_tag.description = description
        source_tag.tombstone = 0
        tags_by_name[target_name] = source_tag
        return source_tag
    if target_tag is None:
        target_tag = queries.create_tag(
            session, target_name, description=description, color=color_hex
        )
        tags_by_name[target_name] = target_tag
    else:
        target_tag.color = color_hex
        target_tag.description = description
        target_tag.tombstone = 0
    if (
        source_tag is not None
        and source_tag is not target_tag
        and _looks_like_ynab_imported_tag(source_tag)
    ):
        source_tag.tombstone = 1
    return target_tag


def _source_flag_tag(
    tags_by_name: dict[str, Any], *, color: str, source_name: str
) -> Any | None:
    candidates = [source_name.strip(), color]
    tags_by_key = {key.lower(): value for key, value in tags_by_name.items()}
    for candidate in candidates:
        if not candidate:
            continue
        tag = tags_by_key.get(candidate.lower())
        if tag is not None and _looks_like_ynab_imported_tag(tag):
            return tag
    return None


def _remove_tag_lookup(tags_by_name: dict[str, Any], tag: Any) -> None:
    tag_name = str(getattr(tag, "tag", "") or "")
    for key in list(tags_by_name):
        if key.lower() == tag_name.lower():
            tags_by_name.pop(key, None)


def _looks_like_ynab_imported_tag(tag: Any) -> bool:
    description = str(getattr(tag, "description", "") or "").strip().lower()
    return description == "imported from ynab"


def _assign_flag_tag_to_transactions(
    transactions: list[Any],
    *,
    expected_count: int,
    source_tag: str,
    target_tag: str,
) -> dict[str, int]:
    updated = 0
    unchanged = 0
    matched = 0
    for transaction in transactions:
        if _deleted(transaction):
            continue
        existing = getattr(transaction, "notes", None)
        notes = "" if existing is None else str(existing)
        migrated = _replace_flag_marker(
            notes, source_tag=source_tag, target_tag=target_tag
        )
        if migrated == notes and not _has_note_tag(notes, target_tag):
            continue
        matched += 1
        if migrated == notes:
            unchanged += 1
            continue
        transaction.notes = migrated
        updated += 1
    total = max(expected_count, matched)
    return {
        "transactions": total,
        "transactions_updated": updated,
        "transactions_missing": max(expected_count - matched, 0),
        "transactions_unchanged": unchanged,
    }


def _all_transactions(session: Any, queries: Any) -> list[Any]:
    by_id: dict[str, Any] = {}
    for is_parent in (False, True):
        for transaction in queries.get_transactions(session, is_parent=is_parent):
            by_id[str(getattr(transaction, "id", len(by_id)))] = transaction
    return list(by_id.values())


def _replace_flag_marker(notes: str, *, source_tag: str, target_tag: str) -> str:
    target_marker = f"#{target_tag}"
    if _has_note_tag(notes, target_tag):
        updated = _remove_flag_marker(notes, source_tag)
        return updated if updated != notes else notes
    return _flag_marker_pattern(source_tag).sub(target_marker, notes)


def _remove_flag_marker(notes: str, source_tag: str) -> str:
    updated = _flag_marker_pattern(source_tag).sub("", notes)
    return re.sub(r"[ \t]{2,}", " ", updated).strip()


def _flag_marker_pattern(source: str) -> re.Pattern[str]:
    escaped = re.escape(source.strip())
    return re.compile(
        rf"(?<![A-Za-z0-9_#])#{escaped}(?![A-Za-z0-9_-])",
        flags=re.IGNORECASE,
    )


def _has_note_tag(notes: str, tag: str) -> bool:
    return _note_tag_pattern(tag).search(notes) is not None


def _note_tag_pattern(tag: str) -> re.Pattern[str]:
    escaped = re.escape(tag.lstrip("#"))
    return re.compile(rf"(?<![A-Za-z0-9_#])#{escaped}(?![A-Za-z0-9_])")


def _append_note_block(existing: str, block: str) -> str:
    existing = existing.rstrip()
    block = block.strip()
    if not existing:
        return block
    return f"{existing}\n\n{block}"


def _note_block_exists(existing: str, block: str) -> bool:
    return block.strip() in {line.strip() for line in existing.splitlines()}


def _rollback_note_block(existing: str, *, before: str, after: str, block: str) -> str:
    if existing == after:
        return before
    if not before and existing.strip() == block.strip():
        return ""
    suffix = f"\n\n{block.strip()}"
    if existing.rstrip().endswith(suffix):
        return existing.rstrip()[: -len(suffix)].rstrip()
    return existing


def _safe_error(message: str, exc: Exception) -> str:
    return f"{message}: {exc.__class__.__name__}"
