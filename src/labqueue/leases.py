"""模拟领取、租约历史和派发裁决。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from labqueue.observations import AdmissionState, WaitingReason


@dataclass(frozen=True, slots=True)
class ClaimedLease:
    claim_id: str
    task_id: str
    gpu_uuid: str
    admission_generation: int
    acquired_version: int


@dataclass(frozen=True, slots=True)
class ClaimBlocked:
    reason: WaitingReason | Literal["queue_empty", "requeue_required"]


class ClaimCommitUnknown(Exception):
    """未能从已提交的租约历史确认领取结果。"""

    def __init__(self, claim_id: str | None) -> None:
        super().__init__("无法确认模拟领取的提交结果")
        self.claim_id = claim_id


ClaimResult = ClaimedLease | ClaimBlocked


@dataclass(frozen=True, slots=True)
class DispatchOptions:
    waiting_reason: WaitingReason | Literal["requeue_required"]
    head_task_id: str | None = None
    gpu_uuid: str | None = None


def initialize_leases(db: sqlite3.Connection) -> None:
    db.execute("""
        CREATE TABLE IF NOT EXISTS gpu_leases (
            claim_id TEXT PRIMARY KEY NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks (task_id),
            gpu_uuid TEXT NOT NULL,
            admission_generation INTEGER NOT NULL,
            acquired_version INTEGER NOT NULL,
            released_version INTEGER CHECK (released_version > acquired_version),
            startup_revoked INTEGER NOT NULL DEFAULT 0 CHECK (startup_revoked IN (0, 1)),
            start_confirmed INTEGER NOT NULL DEFAULT 0 CHECK (start_confirmed IN (0, 1)),
            execution_evidence TEXT NOT NULL DEFAULT 'not_started'
                CHECK (execution_evidence IN ('not_started', 'started', 'unknown')),
            reconciliation_pending INTEGER NOT NULL DEFAULT 0
                CHECK (reconciliation_pending IN (0, 1))
        )
        """)
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS current_lease_by_gpu ON gpu_leases (gpu_uuid)
        WHERE released_version IS NULL
        """)
    db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS current_lease_by_task ON gpu_leases (task_id)
        WHERE released_version IS NULL
        """)


def dispatch_options(db: sqlite3.Connection, state: AdmissionState) -> DispatchOptions:
    """在调用方的同一快照读取队首、回队资格和可用模拟卡。"""
    head = db.execute("""
        SELECT task_id, enqueue_seq FROM tasks WHERE status = 'queued'
        ORDER BY enqueue_seq LIMIT 1
        """).fetchone()
    head_id = head[0] if head is not None else None
    if state.waiting_reason not in ("waiting_dispatch", "gpu_busy"):
        return DispatchOptions(state.waiting_reason, head_id)

    if head is not None and db.execute("""
        SELECT 1 FROM gpu_leases AS lease JOIN tasks AS task USING (task_id)
        WHERE lease.released_version IS NULL AND task.status = 'admitted'
          AND task.enqueue_seq < ? AND lease.start_confirmed = 0
          AND lease.execution_evidence = 'not_started'
          AND (lease.admission_generation <> ? OR lease.startup_revoked = 1
               OR lease.reconciliation_pending = 1)
        LIMIT 1
        """, (head[1], state.admission_generation)).fetchone():
        return DispatchOptions("requeue_required", head_id)

    if not state.candidate_uuids:
        return DispatchOptions("gpu_busy", head_id)
    reserved = {row[0] for row in db.execute(
        "SELECT gpu_uuid FROM gpu_leases WHERE released_version IS NULL"
    )}
    gpu_uuid = next((uuid for uuid in state.candidate_uuids if uuid not in reserved), None)
    return DispatchOptions("waiting_dispatch" if gpu_uuid else "gpu_busy", head_id, gpu_uuid)


def revoke_occupied_leases(db: sqlite3.Connection, occupied_uuids: tuple[str, ...]) -> None:
    db.executemany("""
        UPDATE gpu_leases SET startup_revoked = 1
        WHERE gpu_uuid = ? AND released_version IS NULL AND start_confirmed = 0
          AND startup_revoked = 0
        """, ((gpu_uuid,) for gpu_uuid in occupied_uuids))


def record_claim(
    db: sqlite3.Connection, task_id: str, gpu_uuid: str, admission_generation: int
) -> ClaimedLease:
    version = db.execute("""
        UPDATE queue_state SET snapshot_version = snapshot_version + 1
        WHERE id = 1 RETURNING snapshot_version
        """).fetchone()[0]
    lease = ClaimedLease(str(uuid4()), task_id, gpu_uuid, admission_generation, version)
    inserted = db.execute("""
        INSERT INTO gpu_leases (claim_id, task_id, gpu_uuid, admission_generation, acquired_version)
        VALUES (?, ?, ?, ?, ?)
        """, (lease.claim_id, task_id, gpu_uuid, admission_generation, version)).rowcount
    updated = db.execute("""
        UPDATE tasks SET status = 'admitted' WHERE task_id = ? AND status = 'queued'
        """, (task_id,)).rowcount
    if (inserted, updated) != (1, 1):
        raise sqlite3.IntegrityError("领取时未能同时写入任务状态和租约")
    return lease


def read_claim(db: sqlite3.Connection, claim_id: str) -> ClaimedLease | None:
    row = db.execute("""
        SELECT claim_id, task_id, gpu_uuid, admission_generation, acquired_version
        FROM gpu_leases WHERE claim_id = ?
        """, (claim_id,)).fetchone()
    return ClaimedLease(*row) if row is not None else None
