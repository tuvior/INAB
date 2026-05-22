from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .budget_api import BudgetAccount, BudgetCategory
from .store import Store

MARKER_PREFIX = "INAB YNAB target migration"


@dataclass(frozen=True)
class MigrationWorkspace:
    data_dir: Path

    @property
    def root(self) -> Path:
        return self.data_dir / "ynab" / "migrations"

    def create(self, *, source_budget_id: str, source_budget_name: str) -> str:
        migration_id = (
            datetime.now(tz=UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        state = {
            "id": migration_id,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "source_budget_id": source_budget_id,
            "source_budget_name": source_budget_name,
            "step": "created",
            "decisions": {},
            "category_matches": {},
            "account_matches": {},
        }
        self._dir(migration_id).mkdir(parents=True, exist_ok=False)
        self.save_state(migration_id, state)
        return migration_id

    def state(self, migration_id: str) -> dict[str, Any]:
        path = self._state_path(migration_id)
        if not path.exists():
            raise FileNotFoundError(migration_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def save_state(self, migration_id: str, state: dict[str, Any]) -> None:
        self._dir(migration_id).mkdir(parents=True, exist_ok=True)
        self._state_path(migration_id).write_text(
            json.dumps(state, indent=2, sort_keys=True), encoding="utf-8"
        )

    def save_export(self, migration_id: str, export: dict[str, Any]) -> Path:
        path = self.export_path(migration_id)
        path.write_text(json.dumps(export, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def export(self, migration_id: str) -> dict[str, Any]:
        return json.loads(self.export_path(migration_id).read_text(encoding="utf-8"))

    def export_path(self, migration_id: str) -> Path:
        return self._dir(migration_id) / "ynab-export.json"

    def report_path(self, migration_id: str, name: str) -> Path:
        return self._dir(migration_id) / name

    def _dir(self, migration_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "", migration_id)
        return self.root / safe

    def _state_path(self, migration_id: str) -> Path:
        return self._dir(migration_id) / "state.json"


def budget_from_export(export: dict[str, Any]) -> dict[str, Any]:
    data = export.get("data") if isinstance(export.get("data"), dict) else export
    for key in ("budget", "plan"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, dict):
            return value
    return data if isinstance(data, dict) else {}


def export_counts(export: dict[str, Any]) -> dict[str, int]:
    budget = budget_from_export(export)
    categories = category_sources(export)
    targets = [category for category in categories if category.get("goal_type")]
    return {
        "accounts": len(_list_value(budget, "accounts")),
        "categories": len(categories),
        "payees": len(_list_value(budget, "payees")),
        "transactions": len(_list_value(budget, "transactions")),
        "targets": len(targets),
    }


def category_sources(export: dict[str, Any]) -> list[dict[str, Any]]:
    budget = budget_from_export(export)
    latest_month = _latest_month(budget)
    categories = _list_value(latest_month, "categories") or _list_value(
        budget, "categories"
    )
    category_groups = {
        str(group.get("id")): str(group.get("name") or "")
        for group in _list_value(budget, "category_groups")
    }
    result: list[dict[str, Any]] = []
    for category in categories:
        if bool(category.get("deleted")):
            continue
        item = dict(category)
        group_name = item.get("category_group_name")
        group_id = item.get("category_group_id")
        if not group_name and group_id:
            item["category_group_name"] = category_groups.get(str(group_id), "")
        result.append(item)
    return result


def source_category_map(export: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(category.get("id")): category for category in category_sources(export)}


def analyze_targets(export: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for category in category_sources(export):
        goal_type = str(category.get("goal_type") or "").upper()
        if not goal_type:
            continue
        item = _target_item(category, goal_type)
        items.append(item)
    confidence_order = {
        "exact": 0,
        "approximate": 1,
        "needs_review": 2,
        "unsupported": 3,
    }
    return sorted(
        items,
        key=lambda item: (
            confidence_order.get(item["confidence"], 9),
            item["category_group_name"],
            item["category_name"],
        ),
    )


def default_decisions(items: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    decisions: dict[str, dict[str, str]] = {}
    for item in items:
        action = (
            "active" if item["confidence"] in {"exact", "approximate"} else "comment"
        )
        if item["confidence"] == "unsupported":
            action = "skip"
        decisions[item["category_id"]] = {"action": action, "line": item["line"]}
    return decisions


def build_note_patches(
    *,
    migration_id: str,
    target_items: list[dict[str, Any]],
    decisions: dict[str, dict[str, str]],
    category_matches: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    patches: list[dict[str, str]] = []
    items_by_id = {item["category_id"]: item for item in target_items}
    for source_category_id, match in sorted(category_matches.items()):
        decision = decisions.get(source_category_id) or {}
        action = decision.get("action") or "skip"
        if action == "skip" or not match.get("actual_category_id"):
            continue
        line = (decision.get("line") or "").strip()
        if not line:
            continue
        if action == "comment" and not line.startswith("# "):
            line = f"# {line}"
        item = items_by_id.get(source_category_id, {})
        marker = marker_for(migration_id)
        block = "\n".join(
            [
                f"<!-- {marker} start -->",
                f"<!-- Source YNAB category: {item.get('category_group_name', '')}: {item.get('category_name', '')} -->",
                line,
                f"<!-- {marker} end -->",
            ]
        )
        patches.append(
            {
                "category_id": match["actual_category_id"],
                "category_name": match.get("actual_category_name", ""),
                "source_category_id": source_category_id,
                "block": block,
            }
        )
    return patches


def marker_for(migration_id: str) -> str:
    return f"{MARKER_PREFIX} {migration_id}"


def match_categories(
    export: dict[str, Any], actual_categories: list[BudgetCategory]
) -> dict[str, dict[str, str]]:
    actual_by_key: dict[str, list[BudgetCategory]] = {}
    for category in actual_categories:
        if category.deleted or category.hidden:
            continue
        actual_by_key.setdefault(
            _category_key(category.group_name, category.name), []
        ).append(category)
    matches: dict[str, dict[str, str]] = {}
    for category in category_sources(export):
        key = _category_key(
            str(category.get("category_group_name") or ""),
            str(category.get("name") or ""),
        )
        candidates = actual_by_key.get(key, [])
        if len(candidates) == 1:
            actual = candidates[0]
            matches[str(category.get("id"))] = {
                "actual_category_id": actual.id,
                "actual_category_name": _display_category(actual),
                "status": "exact",
            }
        else:
            matches[str(category.get("id"))] = {
                "actual_category_id": "",
                "actual_category_name": "",
                "status": "ambiguous" if candidates else "missing",
            }
    return matches


def match_accounts(
    ynab_store: Store, actual_accounts: list[BudgetAccount]
) -> dict[str, dict[str, str]]:
    accounts_by_name: dict[str, list[BudgetAccount]] = {}
    for account in actual_accounts:
        if account.deleted or account.closed:
            continue
        accounts_by_name.setdefault(_name_key(account.name), []).append(account)
    matches: dict[str, dict[str, str]] = {}
    for mapping in ynab_store.list_mappings():
        candidates = accounts_by_name.get(_name_key(mapping.account_name), [])
        if len(candidates) == 1:
            account = candidates[0]
            matches[mapping.iban] = {
                "actual_account_id": account.id,
                "actual_account_name": account.name,
                "transfer_payee_id": account.transfer_payee_id or "",
                "status": "exact",
            }
        else:
            matches[mapping.iban] = {
                "actual_account_id": "",
                "actual_account_name": "",
                "transfer_payee_id": "",
                "status": "ambiguous" if candidates else "missing",
            }
    return matches


def migrate_local_state(
    *,
    ynab_store: Store,
    actual_store: Store,
    account_matches: dict[str, dict[str, str]],
    category_matches: dict[str, dict[str, str]],
    source_categories: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "account_mappings": {"copied": [], "skipped": []},
        "rules": {"copied": [], "skipped": []},
        "counterparty_labels": {"copied": []},
        "self_names": {"copied": []},
        "observed_accounts": {"copied": []},
        "observed_counterparties": {"copied": []},
        "not_copied": ["import history", "undo transaction IDs"],
    }
    for mapping in ynab_store.list_mappings():
        match = account_matches.get(mapping.iban) or {}
        account_id = match.get("actual_account_id")
        if not account_id:
            report["account_mappings"]["skipped"].append(
                {"iban": mapping.iban, "reason": match.get("status") or "no match"}
            )
            continue
        actual_store.upsert_account_mapping(
            iban=mapping.iban,
            account_id=account_id,
            account_name=match.get("actual_account_name") or mapping.account_name,
            transfer_payee_id=match.get("transfer_payee_id") or None,
        )
        report["account_mappings"]["copied"].append(mapping.iban)
    for rule in ynab_store.list_rules():
        category_id = None
        category_name = None
        if rule.category_id:
            match = category_matches.get(rule.category_id) or {}
            category_id = match.get("actual_category_id") or None
            category_name = match.get("actual_category_name") or None
            if not category_id:
                source = source_categories.get(rule.category_id) or {}
                report["rules"]["skipped"].append(
                    {
                        "rule": rule.name,
                        "reason": "category not matched",
                        "category": _source_category_label(source),
                    }
                )
                continue
        actual_store.create_rule(
            name=rule.name,
            enabled=rule.enabled,
            operator=rule.operator,
            pattern=rule.pattern,
            replacement_payee=rule.replacement_payee,
            category_id=category_id,
            category_name=category_name,
        )
        report["rules"]["copied"].append(rule.name)
    for mapping in ynab_store.list_counterparty_mappings():
        actual_store.upsert_counterparty_mapping(iban=mapping.iban, label=mapping.label)
        report["counterparty_labels"]["copied"].append(mapping.iban)
    self_names = ynab_store.self_names()
    if self_names:
        actual_store.save_self_names(self_names)
        report["self_names"]["copied"] = self_names
    for observed in ynab_store.list_observed_accounts():
        actual_store.observe_account(
            iban=observed["iban"],
            currency=observed["currency"],
            owner_name=observed.get("owner_name"),
            bank_name=observed.get("bank_name"),
        )
        report["observed_accounts"]["copied"].append(observed["iban"])
    for observed in ynab_store.list_observed_counterparty_accounts():
        actual_store.observe_counterparty_account(
            iban=observed["iban"],
            name=observed.get("name"),
            bank_name=observed.get("bank_name"),
        )
        report["observed_counterparties"]["copied"].append(observed["iban"])
    return report


def markdown_report(report: dict[str, Any]) -> str:
    lines = ["# INAB YNAB-to-Actual Migration Report", ""]
    for section, payload in report.items():
        lines.append(f"## {str(section).replace('_', ' ').title()}")
        if isinstance(payload, dict):
            for key, value in payload.items():
                lines.append(
                    f"- {key.replace('_', ' ')}: {len(value) if isinstance(value, list) else value}"
                )
        elif isinstance(payload, list):
            for value in payload:
                lines.append(f"- {value}")
        else:
            lines.append(f"- {payload}")
        lines.append("")
    return "\n".join(lines)


def _target_item(category: dict[str, Any], goal_type: str) -> dict[str, Any]:
    target = _amount(category.get("goal_target"))
    group_name = str(category.get("category_group_name") or "")
    category_name = str(category.get("name") or "")
    base = {
        "category_id": str(category.get("id")),
        "category_name": category_name,
        "category_group_name": group_name,
        "goal_type": goal_type,
        "goal_target": target,
        "goal_target_month": category.get("goal_target_month"),
        "goal_target_date": category.get("goal_target_date"),
        "goal_cadence": category.get("goal_cadence"),
        "goal_needs_whole_amount": category.get("goal_needs_whole_amount"),
        "confidence": "unsupported",
        "line": "",
        "reason": "Unsupported YNAB target type.",
    }
    if target is None:
        return {**base, "line": "# Unsupported YNAB target: no target amount"}
    if goal_type in {"MF", "MONTHLY_FUNDING"}:
        return {
            **base,
            "confidence": "exact",
            "line": f"#template {target}",
            "reason": "Monthly funding target.",
        }
    if goal_type in {"TB", "TARGET_BALANCE"} and not (
        category.get("goal_target_month") or category.get("goal_target_date")
    ):
        return {
            **base,
            "confidence": "exact",
            "line": f"#goal {target}",
            "reason": "Target balance without date.",
        }
    if goal_type in {"NEED", "NEEDED_SPENDING", "SPENDING"} and _monthly(category):
        needs_whole_amount = category.get("goal_needs_whole_amount")
        if needs_whole_amount is True:
            return {
                **base,
                "confidence": "exact",
                "line": f"#template {target}",
                "reason": "Monthly needed-for-spending target, set aside another.",
            }
        if needs_whole_amount is False:
            return {
                **base,
                "confidence": "exact",
                "line": f"#template up to {target}",
                "reason": "Monthly needed-for-spending target, refill up to.",
            }
        return {
            **base,
            "confidence": "approximate",
            "line": f"#template up to {target}",
            "reason": "Monthly needed-for-spending target with unknown rollover behavior.",
        }
    return {
        **base,
        "confidence": "needs_review",
        "line": f"# Review YNAB {goal_type} target for {target}",
        "reason": "Target has date, cadence, debt, or unsupported semantics.",
    }


def _amount(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return str((Decimal(str(value)) / Decimal("1000")).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return None


def _monthly(category: dict[str, Any]) -> bool:
    cadence = category.get("goal_cadence")
    frequency = category.get("goal_cadence_frequency")
    return cadence in (None, 1, "1") and frequency in (None, 1, "1")


def _latest_month(budget: dict[str, Any]) -> dict[str, Any]:
    months = _list_value(budget, "months")
    if not months:
        return {}
    return sorted(months, key=lambda item: str(item.get("month") or ""))[-1]


def _list_value(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key) if isinstance(data, dict) else None
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _category_key(group_name: str, category_name: str) -> str:
    return f"{_name_key(group_name)}::{_name_key(category_name)}"


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _display_category(category: BudgetCategory) -> str:
    return (
        f"{category.group_name}: {category.name}"
        if category.group_name
        else category.name
    )


def _source_category_label(category: dict[str, Any]) -> str:
    group = str(category.get("category_group_name") or "")
    name = str(category.get("name") or "")
    return f"{group}: {name}" if group else name
