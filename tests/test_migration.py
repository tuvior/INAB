from __future__ import annotations

from inab.budget_api import BudgetAccount, BudgetCategory
from inab.migration import (
    MigrationWorkspace,
    analyze_targets,
    build_note_patches,
    default_decisions,
    match_accounts,
    match_categories,
    migrate_local_state,
    source_category_map,
)
from inab.store import Store


def _export() -> dict:
    return {
        "data": {
            "budget": {
                "category_groups": [{"id": "group-1", "name": "Everyday"}],
                "months": [
                    {
                        "month": "2026-05-01",
                        "categories": [
                            {
                                "id": "cat-food",
                                "category_group_id": "group-1",
                                "name": "Food",
                                "goal_type": "MF",
                                "goal_target": 250000,
                            },
                            {
                                "id": "cat-buffer",
                                "category_group_id": "group-1",
                                "name": "Buffer",
                                "goal_type": "TB",
                                "goal_target": 1000000,
                            },
                            {
                                "id": "cat-holiday",
                                "category_group_id": "group-1",
                                "name": "Holiday",
                                "goal_type": "TBD",
                                "goal_target": 1200000,
                                "goal_target_month": "2026-12-01",
                            },
                            {
                                "id": "cat-gym",
                                "category_group_id": "group-1",
                                "name": "Gym",
                                "goal_type": "NEED",
                                "goal_target": 588000,
                                "goal_needs_whole_amount": True,
                                "goal_cadence": 13,
                                "goal_cadence_frequency": 1,
                                "goal_creation_month": "2026-05-01",
                                "goal_target_month": "2026-06-05",
                            },
                            {
                                "id": "cat-club",
                                "category_group_id": "group-1",
                                "name": "Club",
                                "goal_type": "NEED",
                                "goal_target": 264000,
                                "goal_needs_whole_amount": False,
                                "goal_cadence": 1,
                                "goal_cadence_frequency": 2,
                                "goal_creation_month": "2026-05-01",
                                "goal_target_month": "2026-05-15",
                            },
                            {
                                "id": "cat-utilities",
                                "category_group_id": "group-1",
                                "name": "Utilities",
                                "goal_type": "NEED",
                                "goal_target": 373640,
                                "goal_needs_whole_amount": True,
                            },
                            {
                                "id": "cat-fuel",
                                "category_group_id": "group-1",
                                "name": "Fuel",
                                "goal_type": "NEED",
                                "goal_target": 700000,
                                "goal_needs_whole_amount": False,
                            },
                        ],
                    }
                ],
            }
        }
    }


def test_analyze_targets_groups_template_confidence() -> None:
    items = analyze_targets(_export())

    by_id = {item["category_id"]: item for item in items}
    assert by_id["cat-food"]["confidence"] == "exact"
    assert by_id["cat-food"]["line"] == "#template 250.00"
    assert by_id["cat-buffer"]["line"] == "#goal 1000.00"
    assert by_id["cat-holiday"]["line"] == "#template 1200.00 by 2026-12"
    assert by_id["cat-holiday"]["confidence"] == "exact"
    assert by_id["cat-gym"]["line"] == (
        "#template 588.00 by 2026-06 repeat every year"
    )
    assert by_id["cat-gym"]["confidence"] == "exact"
    assert by_id["cat-club"]["line"] == (
        "#template 264.00 repeat every 2 months starting 2026-05-15 up to 264.00"
    )
    assert by_id["cat-club"]["confidence"] == "approximate"
    assert by_id["cat-utilities"]["line"] == "#template 373.64"
    assert by_id["cat-utilities"]["reason"].endswith("set aside another.")
    assert by_id["cat-fuel"]["line"] == "#template up to 700.00"
    assert by_id["cat-fuel"]["reason"].endswith("refill up to.")


def test_build_note_patches_comments_review_items() -> None:
    items = analyze_targets(_export())
    decisions = default_decisions(items)
    decisions["cat-holiday"] = {
        "action": "comment",
        "line": "#template 100.00",
    }

    patches = build_note_patches(
        migration_id="mig-1",
        target_items=items,
        decisions=decisions,
        category_matches={
            "cat-food": {
                "actual_category_id": "actual-food",
                "actual_category_name": "Everyday: Food",
            },
            "cat-holiday": {
                "actual_category_id": "actual-holiday",
                "actual_category_name": "Everyday: Holiday",
            },
        },
    )

    assert patches[0]["category_id"] == "actual-food"
    assert patches[0]["block"] == "#template 250.00"
    assert "# #template 100.00" in patches[1]["block"]


def test_normalizes_generated_client_plan_export_for_actual_import(tmp_path) -> None:
    workspace = MigrationWorkspace(tmp_path)
    migration_id = workspace.create(
        source_budget_id="plan-1", source_budget_name="Household"
    )

    workspace.save_export(
        migration_id,
        {
            "data": {
                "plan": {"accounts": [{"id": "account-1"}]},
                "server_knowledge": 123,
            }
        },
    )

    export = workspace.export(migration_id)
    assert "budget" in export["data"]
    assert "plan" not in export["data"]
    assert export["data"]["budget"]["accounts"][0]["id"] == "account-1"
    assert export["data"]["server_knowledge"] == 123


def test_match_and_migrate_local_state_skips_history_and_unmatched_rules(
    tmp_path,
) -> None:
    ynab_store = Store(tmp_path / "ynab.sqlite3")
    actual_store = Store(tmp_path / "actual.sqlite3")
    ynab_store.upsert_account_mapping(
        iban="CH111",
        account_id="checking-id",
        account_name="Checking",
        transfer_payee_id="tp-checking",
    )
    ynab_store.create_rule(
        name="Food",
        enabled=True,
        operator="contains",
        pattern="coop",
        replacement_payee="Coop",
        category_id="cat-food",
        category_name="Everyday: Food",
    )
    ynab_store.create_rule(
        name="Unmatched",
        enabled=True,
        operator="contains",
        pattern="holiday",
        replacement_payee=None,
        category_id="cat-holiday",
        category_name="Everyday: Holiday",
    )
    ynab_store.create_job(
        filename="old.xml", status="imported", plan_id="plan-1", payload={}
    )
    actual_categories = [
        BudgetCategory(
            id="actual-food",
            name="Food",
            group_name="Everyday",
            hidden=False,
            deleted=False,
        )
    ]
    actual_accounts = [
        BudgetAccount(
            id="actual-checking",
            name="Checking",
            type="checking",
            closed=False,
            deleted=False,
            transfer_payee_id="actual-tp-checking",
        )
    ]
    category_matches = match_categories(_export(), actual_categories)
    account_matches = match_accounts(ynab_store, actual_accounts)

    report = migrate_local_state(
        ynab_store=ynab_store,
        actual_store=actual_store,
        account_matches=account_matches,
        category_matches=category_matches,
        source_categories=source_category_map(_export()),
    )

    assert actual_store.list_mappings()[0].account_id == "actual-checking"
    assert [rule.name for rule in actual_store.list_rules()] == ["Food"]
    assert actual_store.list_jobs() == []
    assert report["rules"]["skipped"][0]["rule"] == "Unmatched"
