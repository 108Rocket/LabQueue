"""本地模拟执行方上报结束证据，在当前租约上提交结果。"""

import sqlite3
from dataclasses import dataclass
from typing import Literal

from labqueue.auth import Account
from labqueue.starts import read_start

Outcome = Literal["completed", "oom", "failed"]


@dataclass(frozen=True, slots=True)
class EndReport:
    task_id: str
    claim_id: str
    gpu_uuid: str
    outcome: Outcome
    execution_ended: bool
    gpu_use_ended: bool


@dataclass(frozen=True, slots=True)
class EndConfirmed:
    task_id: str
    claim_id: str
    gpu_uuid: str
    outcome: Outcome
    released_version: int


@dataclass(frozen=True, slots=True)
class EndBlocked:
    reason: Literal["execution_unverified", "claim_mismatch", "identity_mismatch", "conflict"]


class EndCommitUnknown(Exception):
    """无法从已提交状态证实本次结果。"""


def initialize_results(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS simulated_results (
            task_id TEXT PRIMARY KEY NOT NULL REFERENCES simulated_start_events (task_id),
            outcome TEXT NOT NULL CHECK (outcome IN ('completed', 'oom', 'failed'))
        )
        """)


def read_result(db: sqlite3.Connection, task_id: str) -> EndConfirmed | None:
    row = db.execute("""
        SELECT s.task_id, s.claim_id, s.gpu_uuid, r.outcome, l.released_version
        FROM simulated_results r JOIN simulated_start_events s USING (task_id)
        JOIN gpu_leases l USING (task_id, claim_id, gpu_uuid)
        WHERE s.task_id = ? AND l.released_version IS NOT NULL
        """, (task_id,)).fetchone()
    return EndConfirmed(*row) if row else None


def decide_end(
    db: sqlite3.Connection, executor: Account, report: EndReport
) -> EndConfirmed | EndBlocked:
    start = read_start(db, report.task_id)
    if start is None:
        return EndBlocked("execution_unverified")
    if (start.claim_id, start.gpu_uuid) != (report.claim_id, report.gpu_uuid):
        return EndBlocked("claim_mismatch")
    if executor.owner_id != start.execution_owner_id:
        return EndBlocked("identity_mismatch")
    if (
        report.execution_ended is not True or report.gpu_use_ended is not True
        or report.outcome not in ("completed", "oom", "failed")
    ):
        return EndBlocked("execution_unverified")

    previous = read_result(db, report.task_id)
    if previous is not None:
        return previous if previous.outcome == report.outcome else EndBlocked("conflict")

    ids = (report.task_id, report.claim_id, report.gpu_uuid)
    lease = db.execute("""
        SELECT 1 FROM gpu_leases JOIN tasks USING (task_id)
        WHERE task_id = ? AND claim_id = ? AND gpu_uuid = ? AND owner_id = ?
          AND status = 'running' AND released_version IS NULL
          AND start_confirmed = 1 AND execution_evidence != 'not_started'
        """, (*ids, executor.owner_id)).fetchone()
    if lease is None:
        return EndBlocked("execution_unverified")

    version = db.execute("""
        UPDATE queue_state SET snapshot_version = snapshot_version + 1
        WHERE id = 1 RETURNING snapshot_version
        """).fetchone()[0]
    status = "completed" if report.outcome == "completed" else "failed"
    recorded = db.execute(
        "INSERT INTO simulated_results (task_id, outcome) VALUES (?, ?)",
        (report.task_id, report.outcome),
    ).rowcount
    updated = db.execute(
        "UPDATE tasks SET status = ? WHERE task_id = ? AND status = 'running'",
        (status, report.task_id),
    ).rowcount
    released = db.execute("""
        UPDATE gpu_leases SET released_version = ?, reconciliation_pending = 0
        WHERE task_id = ? AND claim_id = ? AND gpu_uuid = ? AND released_version IS NULL
        """, (version, *ids)).rowcount
    if (recorded, updated, released) != (1, 1, 1):
        raise sqlite3.IntegrityError("未能同时写入模拟终态与租约释放")
    return EndConfirmed(*ids, report.outcome, version)
