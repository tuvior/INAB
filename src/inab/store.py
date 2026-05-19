from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True)
class AccountMapping:
    iban: str
    ynab_account_id: str
    ynab_account_name: str
    transfer_payee_id: str | None
    updated_at: str


class Store:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                create table if not exists app_config (
                    key text primary key,
                    value text not null
                );

                create table if not exists account_mappings (
                    iban text primary key,
                    ynab_account_id text not null,
                    ynab_account_name text not null,
                    transfer_payee_id text,
                    updated_at text not null
                );

                create table if not exists observed_accounts (
                    iban text primary key,
                    currency text not null,
                    owner_name text,
                    bank_name text,
                    last_seen_at text not null
                );

                create table if not exists import_jobs (
                    id text primary key,
                    filename text not null,
                    status text not null,
                    plan_id text,
                    payload_json text not null,
                    result_json text,
                    created_at text not null,
                    updated_at text not null
                );
                """
            )

    def get_config(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("select value from app_config where key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into app_config (key, value)
                values (?, ?)
                on conflict(key) do update set value = excluded.value
                """,
                (key, value),
            )

    def selected_plan(self) -> tuple[str | None, str | None]:
        return self.get_config("ynab_plan_id"), self.get_config("ynab_plan_name")

    def save_selected_plan(self, plan_id: str, plan_name: str) -> None:
        self.set_config("ynab_plan_id", plan_id)
        self.set_config("ynab_plan_name", plan_name)

    def upsert_mapping(
        self,
        *,
        iban: str,
        ynab_account_id: str,
        ynab_account_name: str,
        transfer_payee_id: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into account_mappings
                    (iban, ynab_account_id, ynab_account_name, transfer_payee_id, updated_at)
                values (?, ?, ?, ?, ?)
                on conflict(iban) do update set
                    ynab_account_id = excluded.ynab_account_id,
                    ynab_account_name = excluded.ynab_account_name,
                    transfer_payee_id = excluded.transfer_payee_id,
                    updated_at = excluded.updated_at
                """,
                (iban.strip().upper(), ynab_account_id, ynab_account_name, transfer_payee_id, now_iso()),
            )

    def delete_mapping(self, iban: str) -> None:
        with self.connect() as connection:
            connection.execute("delete from account_mappings where iban = ?", (iban.strip().upper(),))

    def list_mappings(self) -> list[AccountMapping]:
        with self.connect() as connection:
            rows = connection.execute("select * from account_mappings order by iban").fetchall()
        return [
            AccountMapping(
                iban=row["iban"],
                ynab_account_id=row["ynab_account_id"],
                ynab_account_name=row["ynab_account_name"],
                transfer_payee_id=row["transfer_payee_id"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def mappings_for(self, ibans: set[str]) -> dict[str, AccountMapping]:
        if not ibans:
            return {}
        placeholders = ",".join("?" for _ in ibans)
        with self.connect() as connection:
            rows = connection.execute(
                f"select * from account_mappings where iban in ({placeholders})",
                tuple(sorted(ibans)),
            ).fetchall()
        return {
            row["iban"]: AccountMapping(
                iban=row["iban"],
                ynab_account_id=row["ynab_account_id"],
                ynab_account_name=row["ynab_account_name"],
                transfer_payee_id=row["transfer_payee_id"],
                updated_at=row["updated_at"],
            )
            for row in rows
        }

    def observe_account(self, *, iban: str, currency: str, owner_name: str | None, bank_name: str | None) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into observed_accounts (iban, currency, owner_name, bank_name, last_seen_at)
                values (?, ?, ?, ?, ?)
                on conflict(iban) do update set
                    currency = excluded.currency,
                    owner_name = excluded.owner_name,
                    bank_name = excluded.bank_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (iban.strip().upper(), currency, owner_name, bank_name, now_iso()),
            )

    def list_observed_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("select * from observed_accounts order by last_seen_at desc").fetchall()
        return [dict(row) for row in rows]

    def create_job(self, *, filename: str, status: str, plan_id: str | None, payload: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        timestamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                insert into import_jobs
                    (id, filename, status, plan_id, payload_json, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, filename, status, plan_id, json.dumps(payload), timestamp, timestamp),
            )
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("select * from import_jobs where id = ?", (job_id,)).fetchone()
        if not row:
            return None
        job = dict(row)
        job["payload"] = json.loads(job.pop("payload_json"))
        result_json = job.pop("result_json")
        job["result"] = json.loads(result_json) if result_json else None
        return job

    def update_job(self, job_id: str, *, status: str, payload: dict[str, Any] | None = None, result: dict[str, Any] | None = None) -> None:
        assignments = ["status = ?", "updated_at = ?"]
        values: list[Any] = [status, now_iso()]
        if payload is not None:
            assignments.append("payload_json = ?")
            values.append(json.dumps(payload))
        if result is not None:
            assignments.append("result_json = ?")
            values.append(json.dumps(result))
        values.append(job_id)
        with self.connect() as connection:
            connection.execute(
                f"update import_jobs set {', '.join(assignments)} where id = ?",
                tuple(values),
            )
