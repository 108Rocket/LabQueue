"""持久取消请求。受理与裁决分开提交，封存与两者共用 SQLite 写锁。"""

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    from labqueue.submissions import SubmissionStore

from labqueue.auth import Account
from labqueue.storage import rollback_open_transaction

CancelState = Literal["unknown", "processing", "sealed", "cancelled", "ineligible"]


class CancellationFact(BaseModel):
    request_id: str
    state: CancelState


class CancellationReceipt(CancellationFact):
    task_id: str
    snapshot_version: str


def initialize_cancellations(db: sqlite3.Connection) -> None:
    db.execute("CREATE INDEX IF NOT EXISTS lease_history_by_task ON gpu_leases(task_id)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS cancellation_requests (
            owner_id TEXT NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            request_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('processing','sealed','cancelled','ineligible')),
            accepted_version INTEGER,
            resolved_version INTEGER,
            PRIMARY KEY (owner_id, task_id, request_id)
        )
    """)


CANCEL_ELIGIBLE_SQL = """
    t.status = 'queued'
    AND NOT EXISTS (SELECT 1 FROM gpu_leases WHERE task_id = t.task_id)
    AND NOT EXISTS (SELECT 1 FROM simulated_start_events WHERE task_id = t.task_id)
"""


def cancellation_fact(
    db: sqlite3.Connection, owner_id: str, task_id: str, request_id: str
) -> CancellationFact:
    row = db.execute(
        """
        SELECT state FROM cancellation_requests WHERE owner_id=? AND task_id=? AND request_id=?
    """,
        (owner_id, task_id, request_id),
    ).fetchone()
    return CancellationFact(request_id=request_id, state=row[0] if row else "unknown")


def _receipt(db: sqlite3.Connection, task_id: str, fact: CancellationFact) -> CancellationReceipt:
    version = db.execute("SELECT snapshot_version FROM queue_state WHERE id=1").fetchone()[0]
    return CancellationReceipt(
        task_id=task_id, snapshot_version=str(version), request_id=fact.request_id, state=fact.state
    )


Action = Literal["accept", "seal", "decide"]


def _transition(
    db: sqlite3.Connection, account: Account, task_id: str, fact: CancellationFact, action: Action
) -> None:
    key = (account.owner_id, task_id, fact.request_id)
    if fact.state == "unknown" and action in ("accept", "seal"):
        fact.state = "sealed" if action == "seal" else "processing"
        version_column = "resolved_version" if action == "seal" else "accepted_version"
        db.execute(
            f"""
            INSERT INTO cancellation_requests
                (owner_id, task_id, request_id, state, {version_column})
            SELECT ?, ?, ?, ?, snapshot_version + 1 FROM queue_state WHERE id=1
        """,
            (*key, fact.state),
        )
    elif action == "decide" and fact.state == "processing":
        fact.state = "ineligible"
        if db.execute(
            f"SELECT 1 FROM tasks t WHERE t.task_id = ? AND {CANCEL_ELIGIBLE_SQL}", (task_id,)
        ).fetchone():
            db.execute("UPDATE tasks SET status='cancelled' WHERE task_id=?", (task_id,))
            fact.state = "cancelled"
        db.execute(
            """
            UPDATE cancellation_requests SET state=?,
                resolved_version=(SELECT snapshot_version + 1 FROM queue_state WHERE id=1)
            WHERE owner_id=? AND task_id=? AND request_id=?
        """,
            (fact.state, *key),
        )
    else:
        return
    db.execute("UPDATE queue_state SET snapshot_version = snapshot_version + 1 WHERE id=1")


def _advance_once(
    store: "SubmissionStore", account: Account, task_id: str, request_id: str, action: Action
) -> CancellationReceipt | None:
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        if not store.owns_task(db, account, task_id):
            return None
        fact = cancellation_fact(db, account.owner_id, task_id, request_id)
        _transition(db, account, task_id, fact, action)
        receipt = _receipt(db, task_id, fact)
        try:
            db.execute("COMMIT")
        except sqlite3.Error:
            if rollback_open_transaction(db):
                raise
        else:
            return receipt

    # 提交结果不明。只读核对自身遇到忙锁也不得重跑原写入。
    try:
        known = _read_receipt(store, account, task_id, request_id)
    except sqlite3.Error as error:
        raise sqlite3.DatabaseError("无法读取取消提交结果") from error
    if known and (known.state == "unknown" or (action == "decide" and known.state == "processing")):
        raise sqlite3.DatabaseError("无法证实本次提交")
    return known


def _read_receipt(
    store: "SubmissionStore", account: Account, task_id: str, request_id: str
) -> CancellationReceipt | None:
    with closing(store._connect_readonly()) as db:
        db.execute("BEGIN")
        if not store.owns_task(db, account, task_id):
            return None
        known = cancellation_fact(db, account.owner_id, task_id, request_id)
        return _receipt(db, task_id, known)


def _advance(
    store: "SubmissionStore", account: Account, task_id: str, request_id: str, action: Action
) -> CancellationReceipt | None:
    try:
        return _advance_once(store, account, task_id, request_id, action)
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY:
            raise
        return _advance_once(store, account, task_id, request_id, action)


def cancel(
    store: "SubmissionStore", account: Account, task_id: str, request_id: str, *, seal: bool = False
) -> CancellationReceipt | None:
    known = _read_receipt(store, account, task_id, request_id)
    if known is None or known.state not in ("processing", "unknown"):
        return known
    receipt = _advance(store, account, task_id, request_id, "seal" if seal else "accept")
    if not seal and receipt and receipt.state == "processing":
        return _advance(store, account, task_id, request_id, "decide")
    return receipt


def recover_cancellations(store: "SubmissionStore") -> None:
    with closing(store._connect_readonly()) as db:
        pending = db.execute("""
            SELECT r.owner_id, t.group_id, r.task_id, r.request_id
            FROM cancellation_requests r JOIN tasks t USING (owner_id, task_id)
            WHERE r.state='processing'
        """).fetchall()
    for owner_id, group_id, task_id, request_id in pending:
        _advance(store, Account(owner_id, group_id), task_id, request_id, "decide")
