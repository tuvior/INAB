from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import BankTransaction, normalize_whitespace
from .store import ImportRule

RULE_OPERATORS = {"contains", "exact", "starts_with", "regex"}


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class RuleInput:
    name: str
    enabled: bool
    operator: str
    pattern: str
    replacement_payee: str | None
    category_id: str | None
    category_name: str | None


@dataclass(frozen=True)
class RuleEvaluation:
    matched_rule_id: str | None
    matched_rule_name: str | None
    output_payee: str
    output_category_id: str | None
    output_category_name: str | None
    original_payee: str | None

    @property
    def matched(self) -> bool:
        return self.matched_rule_id is not None


def validate_rule_input(
    *,
    name: str,
    enabled: bool,
    operator: str,
    pattern: str,
    replacement_payee: str | None,
    category_id: str | None,
    category_name: str | None,
) -> RuleInput:
    cleaned_name = normalize_whitespace(name)
    cleaned_pattern = normalize_whitespace(pattern)
    cleaned_payee = normalize_whitespace(replacement_payee)
    cleaned_category_id = normalize_whitespace(category_id)
    cleaned_category_name = normalize_whitespace(category_name)
    if not cleaned_name:
        raise RuleError("Rule name is required.")
    if operator not in RULE_OPERATORS:
        raise RuleError("Rule operator is not supported.")
    if not cleaned_pattern:
        raise RuleError("Rule pattern is required.")
    if operator == "regex":
        try:
            re.compile(cleaned_pattern, flags=re.IGNORECASE)
        except re.error as exc:
            raise RuleError(f"Invalid regular expression: {exc}") from exc
    if not cleaned_payee and not cleaned_category_id:
        raise RuleError("A rule must set a replacement payee, a category, or both.")
    if bool(cleaned_category_id) != bool(cleaned_category_name):
        raise RuleError("Selected category is invalid.")
    return RuleInput(
        name=cleaned_name[:120],
        enabled=enabled,
        operator=operator,
        pattern=cleaned_pattern[:300],
        replacement_payee=cleaned_payee[:200] or None,
        category_id=cleaned_category_id or None,
        category_name=cleaned_category_name[:200] or None,
    )


def apply_rules(transactions: list[BankTransaction], rules: list[ImportRule]) -> None:
    enabled_rules = [rule for rule in rules if rule.enabled]
    if not enabled_rules:
        return
    for tx in transactions:
        for rule in enabled_rules:
            if _matches(rule, tx):
                _apply_rule(tx, rule)
                break


def evaluate_transaction(payee: str, memo: str | None, rules: list[ImportRule]) -> RuleEvaluation:
    tx = BankTransaction(
        uid="rule-test",
        statement_id="rule-test",
        iban="RULETEST",
        currency="CHF",
        booking_date=date(2000, 1, 1),
        value_date=None,
        amount=Decimal("0"),
        payee=normalize_whitespace(payee) or "Unknown payee",
        memo=normalize_whitespace(memo) or None,
        source_ref=None,
        import_id="RULETEST",
        sequence=1,
    )
    apply_rules([tx], rules)
    return RuleEvaluation(
        matched_rule_id=tx.applied_rule_id,
        matched_rule_name=tx.applied_rule_name,
        output_payee=tx.payee,
        output_category_id=tx.category_id,
        output_category_name=tx.category_name,
        original_payee=tx.original_payee,
    )


def _matches(rule: ImportRule, tx: BankTransaction) -> bool:
    target = _target_text(tx)
    pattern = normalize_whitespace(rule.pattern)
    if rule.operator == "regex":
        return re.search(pattern, target, flags=re.IGNORECASE) is not None
    folded_target = target.casefold()
    folded_pattern = pattern.casefold()
    if rule.operator == "contains":
        return folded_pattern in folded_target
    if rule.operator == "exact":
        return folded_target == folded_pattern
    if rule.operator == "starts_with":
        return folded_target.startswith(folded_pattern)
    return False


def _apply_rule(tx: BankTransaction, rule: ImportRule) -> None:
    tx.applied_rule_id = rule.id
    tx.applied_rule_name = rule.name
    if rule.replacement_payee:
        if tx.original_payee is None and tx.payee != rule.replacement_payee:
            tx.original_payee = tx.payee
        tx.payee = rule.replacement_payee[:200]
    if rule.category_id:
        tx.category_id = rule.category_id
        tx.category_name = rule.category_name


def _target_text(tx: BankTransaction) -> str:
    return normalize_whitespace("\n".join(part for part in (tx.payee, tx.memo) if part))
