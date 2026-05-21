from __future__ import annotations

from datetime import UTC, datetime, timedelta

from inab.store import Store


def _age_job(store: Store, job_id: str, *, days: int) -> None:
    timestamp = (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()
    with store.connect() as connection:
        connection.execute(
            "update import_jobs set created_at = ?, updated_at = ? where id = ?",
            (timestamp, timestamp, job_id),
        )


def test_store_lists_jobs_newest_first(tmp_path) -> None:
    store = Store(tmp_path / "inab.sqlite3")
    older = store.create_job(
        filename="older.xml",
        status="preview",
        plan_id="plan-1",
        payload={"transaction_count": 1},
    )
    newer = store.create_job(
        filename="newer.xml",
        status="imported",
        plan_id="plan-1",
        payload={"transaction_count": 2},
    )
    _age_job(store, older, days=2)

    jobs = store.list_jobs()

    assert [job["id"] for job in jobs] == [newer, older]
    assert jobs[0]["payload"]["transaction_count"] == 2


def test_store_prunes_only_stale_uncommitted_jobs(tmp_path) -> None:
    store = Store(tmp_path / "inab.sqlite3")
    stale_preview = store.create_job(
        filename="stale.xml", status="preview", plan_id="plan-1", payload={}
    )
    fresh_preview = store.create_job(
        filename="fresh.xml", status="preview", plan_id="plan-1", payload={}
    )
    imported = store.create_job(
        filename="imported.xml", status="imported", plan_id="plan-1", payload={}
    )
    failed_with_created_ids = store.create_job(
        filename="partial.xml", status="failed", plan_id="plan-1", payload={}
    )
    _age_job(store, stale_preview, days=8)
    _age_job(store, imported, days=8)
    _age_job(store, failed_with_created_ids, days=8)
    store.update_job(
        failed_with_created_ids, status="failed", result={"transaction_ids": ["ynab-1"]}
    )
    with store.connect() as connection:
        old = (datetime.now(tz=UTC) - timedelta(days=8)).isoformat()
        connection.execute(
            "update import_jobs set created_at = ?, updated_at = ? where id = ?",
            (old, old, failed_with_created_ids),
        )

    pruned = store.prune_stale_uncommitted_jobs(older_than_days=7)

    remaining_ids = {job["id"] for job in store.list_jobs()}
    assert pruned == 1
    assert stale_preview not in remaining_ids
    assert fresh_preview in remaining_ids
    assert imported in remaining_ids
    assert failed_with_created_ids in remaining_ids
