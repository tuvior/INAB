from __future__ import annotations

from inab.rules import RuleError, apply_rules, validate_rule_input
from inab.store import Store

from conftest import tx


def test_store_rules_create_update_move_delete(tmp_path) -> None:
    store = Store(tmp_path / "inab.sqlite3")
    first = store.create_rule(
        name="Food",
        enabled=True,
        operator="contains",
        pattern="coop",
        replacement_payee="Coop",
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    second = store.create_rule(
        name="Travel",
        enabled=False,
        operator="starts_with",
        pattern="sbb",
        replacement_payee=None,
        category_id="cat-travel",
        category_name="Everyday: Travel",
    )

    assert [rule.id for rule in store.list_rules()] == [first, second]
    assert [rule.id for rule in store.list_rules(enabled_only=True)] == [first]

    store.move_rule(second, "up")
    assert [rule.id for rule in store.list_rules()] == [second, first]

    store.update_rule(
        second,
        name="Train",
        enabled=True,
        operator="regex",
        pattern="sbb|cff",
        replacement_payee="SBB",
        category_id=None,
        category_name=None,
    )
    updated = store.list_rules()[0]
    assert updated.name == "Train"
    assert updated.enabled is True
    assert updated.operator == "regex"

    store.delete_rule(first)
    assert [rule.id for rule in store.list_rules()] == [second]


def test_rule_engine_operators_and_first_match_wins(tmp_path) -> None:
    store = Store(tmp_path / "inab.sqlite3")
    disabled = store.create_rule(
        name="Disabled",
        enabled=False,
        operator="contains",
        pattern="coop",
        replacement_payee="Wrong",
        category_id=None,
        category_name=None,
    )
    exact = store.create_rule(
        name="Exact",
        enabled=True,
        operator="exact",
        pattern="Payee INAB:1\nMemo INAB:1",
        replacement_payee="Exact match",
        category_id=None,
        category_name=None,
    )
    contains = store.create_rule(
        name="Contains",
        enabled=True,
        operator="contains",
        pattern="memo",
        replacement_payee="Contains match",
        category_id=None,
        category_name=None,
    )
    store.move_rule(exact, "up")
    store.move_rule(exact, "up")
    assert disabled

    transaction = tx("INAB:1", "CH111", "-10.00")
    apply_rules([transaction], store.list_rules())

    assert transaction.payee == "Exact match"
    assert transaction.original_payee == "Payee INAB:1"
    assert transaction.applied_rule_id == exact
    assert transaction.applied_rule_name == "Exact"

    transaction = tx("INAB:2", "CH111", "-10.00")
    apply_rules([transaction], store.list_rules())
    assert transaction.payee == "Contains match"
    assert transaction.applied_rule_id == contains


def test_rule_engine_starts_with_regex_and_category_assignment(tmp_path) -> None:
    store = Store(tmp_path / "inab.sqlite3")
    starts = store.create_rule(
        name="Starts",
        enabled=True,
        operator="starts_with",
        pattern="Payee",
        replacement_payee=None,
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    regex = store.create_rule(
        name="Regex",
        enabled=True,
        operator="regex",
        pattern="unused",
        replacement_payee="Wrong",
        category_id=None,
        category_name=None,
    )
    assert regex

    transaction = tx("INAB:1", "CH111", "-10.00")
    apply_rules([transaction], store.list_rules())

    assert transaction.payee == "Payee INAB:1"
    assert transaction.original_payee is None
    assert transaction.category_id == "cat-food"
    assert transaction.category_name == "Everyday: Food"
    assert transaction.applied_rule_id == starts


def test_validate_rule_rejects_invalid_input() -> None:
    try:
        validate_rule_input(
            name="Bad",
            enabled=True,
            operator="regex",
            pattern="[",
            replacement_payee="Payee",
            category_id=None,
            category_name=None,
        )
    except RuleError as exc:
        assert "Invalid regular expression" in str(exc)
    else:
        raise AssertionError("Expected invalid regex to fail")

    try:
        validate_rule_input(
            name="No action",
            enabled=True,
            operator="contains",
            pattern="coop",
            replacement_payee="",
            category_id=None,
            category_name=None,
        )
    except RuleError as exc:
        assert "must set" in str(exc)
    else:
        raise AssertionError("Expected rule without action to fail")
