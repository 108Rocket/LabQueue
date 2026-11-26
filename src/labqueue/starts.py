"""本地模拟启动的裁决及持久回执。"""

import sqlite3
from dataclasses import dataclass
from typing import Literal

from labqueue.auth import Account
from labqueue.observations import read_admission_for_dispatch


@dataclass(frozen=True, slots=True)
class StartConfirmed:
    task_id: str
    claim_id: str
    gpu_uuid: str
    execution_owner_id: str
    started_at: int
    snapshot_version: int


@dataclass(frozen=True, slots=True)
class StartBlocked:
    reason: Literal[
        "claim_mismatch", "identity_mismatch", "task_not_admitted", "reconciliation_pending",
        "execution_unverified", "observation_blocked", "claim_invalidated",
    ]


class StartCommitUnknown(Exception):
    """无法从已提交状态证实本次启动确认。"""


StartResult = StartConfirmed | StartBlocked


def initialize_starts(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS simulated_start_events (
            task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks (task_id),
            claim_id TEXT NOT NULL REFERENCES gpu_leases (claim_id),
            gpu_uuid TEXT NOT NULL,
            execution_owner_id TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            snapshot_version INTEGER NOT NULL
        )
        """)


def read_start(db: sqlite3.Connection, task_id: str) -> StartConfirmed | None:
    row = db.execute("""
        SELECT task_id, claim_id, gpu_uuid, execution_owner_id, started_at, snapshot_version
        FROM simulated_start_events WHERE task_id = ?
        """, (task_id,)).fetchone()
    return StartConfirmed(*row) if row is not None else None


def decide_start(
    db: sqlite3.Connection, executor: Account, task_id: str, claim_id: str, gpu_uuid: str
) -> StartResult:
    """在调用方持有的写事务中裁决启动。"""
    current = db.execute("""
        SELECT task.owner_id, task.status, lease.admission_generation, lease.startup_revoked,
               lease.reconciliation_pending, lease.start_confirmed, lease.execution_evidence
        FROM gpu_leases AS lease JOIN tasks AS task USING (task_id)
        WHERE lease.task_id = ? AND lease.claim_id = ? AND lease.gpu_uuid = ?
          AND lease.released_version IS NULL
        """, (task_id, claim_id, gpu_uuid)).fetchone()
    if current is None:
        return StartBlocked("claim_mismatch")
    if executor.owner_id != current["owner_id"]:
        return StartBlocked("identity_mismatch")
    if current["status"] not in ("admitted", "running"):
        return StartBlocked("task_not_admitted")

    event = read_start(db, task_id)
    if event is not None:
        # 已证实的启动确认可读回，即使后续执行结果进入待核对。
        if (
            current["status"] == "running"
            and current["start_confirmed"] == 1
            and current["execution_evidence"] != "not_started"
            and (event.claim_id, event.gpu_uuid, event.execution_owner_id)
            == (claim_id, gpu_uuid, executor.owner_id)
        ):
            return event
        return StartBlocked("execution_unverified")
    if current["reconciliation_pending"]:
        return StartBlocked("reconciliation_pending")
    if current["start_confirmed"] or current["execution_evidence"] != "not_started":
        return StartBlocked("execution_unverified")
    if current["status"] != "admitted":
        return StartBlocked("task_not_admitted")

    admission = read_admission_for_dispatch(db)
    if admission.waiting_reason != "waiting_dispatch" or admission.deadline is None:
        return StartBlocked("observation_blocked")
    if (
        current["admission_generation"] != admission.admission_generation
        or current["startup_revoked"]
        or gpu_uuid not in admission.candidate_uuids
    ):
        return StartBlocked("claim_invalidated")

    version = db.execute("""
        UPDATE queue_state SET snapshot_version = snapshot_version + 1
        WHERE id = 1 RETURNING snapshot_version
        """).fetchone()[0]
    confirmed = db.execute("""
        UPDATE gpu_leases SET start_confirmed = 1, execution_evidence = 'started'
        WHERE claim_id = ?
        """, (claim_id,)).rowcount
    inserted = db.execute("""
        INSERT INTO simulated_start_events
            (task_id, claim_id, gpu_uuid, execution_owner_id, started_at, snapshot_version)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (task_id, claim_id, gpu_uuid, executor.owner_id, admission.clock, version)).rowcount
    running = db.execute("""
        UPDATE tasks SET status = 'running' WHERE task_id = ?
        """, (task_id,)).rowcount
    if (confirmed, inserted, running) != (1, 1, 1):
        raise sqlite3.IntegrityError("未能同时写入模拟启动确认、事件和任务状态")
    return StartConfirmed(task_id, claim_id, gpu_uuid, executor.owner_id, admission.clock, version)
