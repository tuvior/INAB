from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

    @property
    def account_id(self) -> str:
        return self.ynab_account_id

    @property
    def account_name(self) -> str:
        return self.ynab_account_name


@dataclass(frozen=True)
class CounterpartyAccountMapping:
    iban: str
    label: str
    updated_at: str


@dataclass(frozen=True)
class ImportRule:
    id: str
    name: str
    enabled: bool
    priority: int
    operator: str
    pattern: str
    replacement_payee: str | None
    category_id: str | None
    category_name: str | None
    created_at: str
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
            connection.executescript("""
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

                create table if not exists observed_counterparty_accounts (
                    iban text primary key,
                    name text,
                    bank_name text,
                    last_seen_at text not null
                );

                create table if not exists dismissed_observed_accounts (
                    iban text primary key,
                    dismissed_at text not null
                );

                create table if not exists dismissed_observed_counterparty_accounts (
                    iban text primary key,
                    dismissed_at text not null
                );

                create table if not exists counterparty_account_mappings (
                    iban text primary key,
                    label text not null,
                    updated_at text not null
                );

                create table if not exists import_rules (
                    id text primary key,
                    name text not null,
                    enabled integer not null,
                    priority integer not null,
                    operator text not null,
                    pattern text not null,
                    replacement_payee text,
                    category_id text,
                    category_name text,
                    created_at text not null,
                    updated_at text not null
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
                """)

    def get_config(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "select value from app_config where key = ?", (key,)
            ).fetchone()
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

    def self_names(self) -> list[str]:
        raw = self.get_config("self_names")
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def save_self_names(self, names: list[str]) -> None:
        cleaned = [name.strip() for name in names if name.strip()]
        self.set_config("self_names", json.dumps(cleaned))

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
                (
                    iban.strip().upper(),
                    ynab_account_id,
                    ynab_account_name,
                    transfer_payee_id,
                    now_iso(),
                ),
            )

    def upsert_account_mapping(
        self,
        *,
        iban: str,
        account_id: str,
        account_name: str,
        transfer_payee_id: str | None,
    ) -> None:
        self.upsert_mapping(
            iban=iban,
            ynab_account_id=account_id,
            ynab_account_name=account_name,
            transfer_payee_id=transfer_payee_id,
        )

    def delete_mapping(self, iban: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "delete from account_mappings where iban = ?", (iban.strip().upper(),)
            )

    def list_mappings(self) -> list[AccountMapping]:
        with self.connect() as connection:
            rows = connection.execute(
                "select * from account_mappings order by iban"
            ).fetchall()
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

    def observe_account(
        self, *, iban: str, currency: str, owner_name: str | None, bank_name: str | None
    ) -> None:
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
            rows = connection.execute(
                "select * from observed_accounts order by last_seen_at desc"
            ).fetchall()
        return [dict(row) for row in rows]

    def dismiss_observed_account(self, iban: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into dismissed_observed_accounts (iban, dismissed_at)
                values (?, ?)
                on conflict(iban) do update set dismissed_at = excluded.dismissed_at
                """,
                (iban.strip().upper(), now_iso()),
            )

    def dismissed_observed_account_ibans(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "select iban from dismissed_observed_accounts"
            ).fetchall()
        return {row["iban"] for row in rows}

    def observe_counterparty_account(
        self, *, iban: str, name: str | None, bank_name: str | None
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into observed_counterparty_accounts (iban, name, bank_name, last_seen_at)
                values (?, ?, ?, ?)
                on conflict(iban) do update set
                    name = coalesce(excluded.name, observed_counterparty_accounts.name),
                    bank_name = coalesce(excluded.bank_name, observed_counterparty_accounts.bank_name),
                    last_seen_at = excluded.last_seen_at
                """,
                (iban.strip().upper(), name, bank_name, now_iso()),
            )

    def list_observed_counterparty_accounts(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "select * from observed_counterparty_accounts order by last_seen_at desc"
            ).fetchall()
        return [dict(row) for row in rows]

    def dismiss_observed_counterparty_account(self, iban: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into dismissed_observed_counterparty_accounts (iban, dismissed_at)
                values (?, ?)
                on conflict(iban) do update set dismissed_at = excluded.dismissed_at
                """,
                (iban.strip().upper(), now_iso()),
            )

    def dismissed_observed_counterparty_ibans(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "select iban from dismissed_observed_counterparty_accounts"
            ).fetchall()
        return {row["iban"] for row in rows}

    def upsert_counterparty_mapping(self, *, iban: str, label: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                insert into counterparty_account_mappings (iban, label, updated_at)
                values (?, ?, ?)
                on conflict(iban) do update set
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (iban.strip().upper(), label.strip(), now_iso()),
            )

    def delete_counterparty_mapping(self, iban: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "delete from counterparty_account_mappings where iban = ?",
                (iban.strip().upper(),),
            )

    def list_counterparty_mappings(self) -> list[CounterpartyAccountMapping]:
        with self.connect() as connection:
            rows = connection.execute(
                "select * from counterparty_account_mappings order by label, iban"
            ).fetchall()
        return [
            CounterpartyAccountMapping(
                iban=row["iban"],
                label=row["label"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def counterparty_mappings_for(
        self, ibans: set[str]
    ) -> dict[str, CounterpartyAccountMapping]:
        if not ibans:
            return {}
        placeholders = ",".join("?" for _ in ibans)
        with self.connect() as connection:
            rows = connection.execute(
                f"select * from counterparty_account_mappings where iban in ({placeholders})",
                tuple(sorted(ibans)),
            ).fetchall()
        return {
            row["iban"]: CounterpartyAccountMapping(
                iban=row["iban"],
                label=row["label"],
                updated_at=row["updated_at"],
            )
            for row in rows
        }

    def create_rule(
        self,
        *,
        name: str,
        enabled: bool,
        operator: str,
        pattern: str,
        replacement_payee: str | None,
        category_id: str | None,
        category_name: str | None,
    ) -> str:
        rule_id = uuid.uuid4().hex
        timestamp = now_iso()
        with self.connect() as connection:
            row = connection.execute(
                "select coalesce(max(priority), 0) as max_priority from import_rules"
            ).fetchone()
            priority = int(row["max_priority"] or 0) + 100
            connection.execute(
                """
                insert into import_rules
                    (id, name, enabled, priority, operator, pattern, replacement_payee, category_id, category_name, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    name,
                    1 if enabled else 0,
                    priority,
                    operator,
                    pattern,
                    replacement_payee,
                    category_id,
                    category_name,
                    timestamp,
                    timestamp,
                ),
            )
        return rule_id

    def update_rule(
        self,
        rule_id: str,
        *,
        name: str,
        enabled: bool,
        operator: str,
        pattern: str,
        replacement_payee: str | None,
        category_id: str | None,
        category_name: str | None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                update import_rules set
                    name = ?,
                    enabled = ?,
                    operator = ?,
                    pattern = ?,
                    replacement_payee = ?,
                    category_id = ?,
                    category_name = ?,
                    updated_at = ?
                where id = ?
                """,
                (
                    name,
                    1 if enabled else 0,
                    operator,
                    pattern,
                    replacement_payee,
                    category_id,
                    category_name,
                    now_iso(),
                    rule_id,
                ),
            )

    def delete_rule(self, rule_id: str) -> None:
        with self.connect() as connection:
            connection.execute("delete from import_rules where id = ?", (rule_id,))

    def move_rule(self, rule_id: str, direction: str) -> None:
        if direction not in {"up", "down"}:
            return
        with self.connect() as connection:
            current = connection.execute(
                "select id, priority from import_rules where id = ?", (rule_id,)
            ).fetchone()
            if not current:
                return
            comparator = "<" if direction == "up" else ">"
            ordering = "desc" if direction == "up" else "asc"
            swap = connection.execute(
                f"""
                select id, priority from import_rules
                where priority {comparator} ?
                order by priority {ordering}
                limit 1
                """,
                (current["priority"],),
            ).fetchone()
            if not swap:
                return
            timestamp = now_iso()
            connection.execute(
                "update import_rules set priority = ?, updated_at = ? where id = ?",
                (swap["priority"], timestamp, current["id"]),
            )
            connection.execute(
                "update import_rules set priority = ?, updated_at = ? where id = ?",
                (current["priority"], timestamp, swap["id"]),
            )

    def list_rules(self, *, enabled_only: bool = False) -> list[ImportRule]:
        sql = "select * from import_rules"
        params: tuple[Any, ...] = ()
        if enabled_only:
            sql += " where enabled = ?"
            params = (1,)
        sql += " order by priority, created_at"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_rule_from_row(row) for row in rows]

    def create_job(
        self,
        *,
        filename: str,
        status: str,
        plan_id: str | None,
        payload: dict[str, Any],
    ) -> str:
        job_id = uuid.uuid4().hex
        timestamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                insert into import_jobs
                    (id, filename, status, plan_id, payload_json, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    filename,
                    status,
                    plan_id,
                    json.dumps(payload),
                    timestamp,
                    timestamp,
                ),
            )
        return job_id

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "select * from import_jobs where id = ?", (job_id,)
            ).fetchone()
        if not row:
            return None
        return _job_from_row(row)

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                select * from import_jobs
                order by created_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [_job_from_row(row) for row in rows]

    def prune_stale_uncommitted_jobs(self, *, older_than_days: int = 7) -> int:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=older_than_days)).isoformat()
        with self.connect() as connection:
            rows = connection.execute(
                """
                select id, result_json from import_jobs
                where status in ('preview', 'blocked', 'failed')
                  and created_at < ?
                """,
                (cutoff,),
            ).fetchall()
            stale_ids = [
                row["id"]
                for row in rows
                if not _result_created_transaction_ids(row["result_json"])
            ]
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                connection.execute(
                    f"delete from import_jobs where id in ({placeholders})",
                    tuple(stale_ids),
                )
        return len(stale_ids)

    def update_job(
        self,
        job_id: str,
        *,
        status: str,
        payload: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
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


def _job_from_row(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["payload"] = json.loads(job.pop("payload_json"))
    result_json = job.pop("result_json")
    job["result"] = json.loads(result_json) if result_json else None
    return job


def _result_created_transaction_ids(result_json: str | None) -> list[str]:
    if not result_json:
        return []
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        return []
    transaction_ids = (
        result.get("transaction_ids") if isinstance(result, dict) else None
    )
    if not isinstance(transaction_ids, list):
        return []
    return [
        str(transaction_id) for transaction_id in transaction_ids if str(transaction_id)
    ]


def _rule_from_row(row: sqlite3.Row) -> ImportRule:
    return ImportRule(
        id=row["id"],
        name=row["name"],
        enabled=bool(row["enabled"]),
        priority=int(row["priority"]),
        operator=row["operator"],
        pattern=row["pattern"],
        replacement_payee=row["replacement_payee"],
        category_id=row["category_id"],
        category_name=row["category_name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
