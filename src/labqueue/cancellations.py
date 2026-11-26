"""持久取消请求。受理与裁决分开提交，封存与两者共用 SQLite 写锁。"""

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from labqueue.submissions import SubmissionStore

from labqueue.auth import Account
from labqueue.starts import read_start
from labqueue.storage import rollback_open_transaction

CancelState = Literal["unknown", "processing", "sealed", "cancelled", "ineligible"]


class CancellationFact(BaseModel):
    request_id: str
    state: CancelState
    reason: Literal["past_cutoff"] | None = Field(default=None, exclude_if=lambda v: v is None)


class CancellationReceipt(CancellationFact):
    task_id: str
    snapshot_version: str


class CancellationDeferred(sqlite3.DatabaseError):
    """该请求的失败已持久隔离，服务可继续恢复其他请求。"""


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
    if "reason" not in {row[1] for row in db.execute("PRAGMA table_info(cancellation_requests)")}:
        db.execute("ALTER TABLE cancellation_requests ADD COLUMN reason TEXT")
    db.execute("""
        CREATE TABLE IF NOT EXISTS cancellation_recovery_blocks (
            owner_id TEXT NOT NULL, task_id TEXT NOT NULL, request_id TEXT NOT NULL,
            PRIMARY KEY (owner_id, task_id, request_id),
            FOREIGN KEY (owner_id, task_id, request_id)
                REFERENCES cancellation_requests(owner_id, task_id, request_id)
        )
    """)


BASE_CANCEL_ELIGIBLE_SQL = """
    ((t.status = 'queued'
      AND NOT EXISTS (SELECT 1 FROM gpu_leases WHERE task_id = t.task_id))
     OR (t.status = 'admitted' AND EXISTS (
        SELECT 1 FROM gpu_leases l WHERE l.task_id = t.task_id
          AND l.released_version IS NULL AND l.start_confirmed = 0
          AND l.execution_evidence = 'not_started' AND l.reconciliation_pending = 0)))
    AND NOT EXISTS (SELECT 1 FROM simulated_start_events WHERE task_id = t.task_id)
"""
CANCEL_ELIGIBLE_SQL = BASE_CANCEL_ELIGIBLE_SQL + """
    AND NOT EXISTS (SELECT 1 FROM cancellation_recovery_blocks WHERE task_id = t.task_id)
"""


def cancellation_fact(
    db: sqlite3.Connection, owner_id: str, task_id: str, request_id: str
) -> CancellationFact:
    row = db.execute(
        """
        SELECT state, reason FROM cancellation_requests
        WHERE owner_id=? AND task_id=? AND request_id=?
    """,
        (owner_id, task_id, request_id),
    ).fetchone()
    return CancellationFact(
        request_id=request_id, state=row[0] if row else "unknown", reason=row[1] if row else None
    )


def _receipt(db: sqlite3.Connection, task_id: str, fact: CancellationFact) -> CancellationReceipt:
    version = db.execute("SELECT snapshot_version FROM queue_state WHERE id=1").fetchone()[0]
    return CancellationReceipt(
        task_id=task_id, snapshot_version=str(version), **fact.model_dump()
    )


Action = Literal["accept", "seal", "decide"]


def _decide(db: sqlite3.Connection, account: Account, task_id: str) -> tuple[str, str | None]:
    """以完整匹配的执行证据裁决取消，证据不明时保留原领取。"""
    status = db.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]
    event = read_start(db, task_id)
    if event is not None:
        confirmed = db.execute("""
            SELECT 1 FROM gpu_leases WHERE task_id=? AND claim_id=? AND gpu_uuid=?
              AND start_confirmed=1 AND execution_evidence != 'not_started'
              AND (? != 'running' OR released_version IS NULL)
        """, (task_id, event.claim_id, event.gpu_uuid, status)).fetchone()
        if (
            confirmed and status in ("running", "completed", "failed")
            and event.task_id == task_id and event.execution_owner_id == account.owner_id
        ):
            return "ineligible", "past_cutoff"
        raise sqlite3.DatabaseError("启动证据不完整，保留原领取待核对")
    if status in ("admitted", "running"):
        lease = db.execute("""
            SELECT claim_id FROM gpu_leases WHERE task_id=? AND released_version IS NULL
              AND start_confirmed=0 AND execution_evidence='not_started'
              AND reconciliation_pending=0
        """, (task_id,)).fetchone()
        if status != "admitted" or lease is None:
            raise sqlite3.DatabaseError("无法证实尚未启动，保留原领取待核对")
        released = db.execute("""
            UPDATE gpu_leases SET startup_revoked=1,
                released_version=(SELECT snapshot_version+1 FROM queue_state WHERE id=1)
            WHERE claim_id=? AND task_id=? AND released_version IS NULL
              AND start_confirmed=0 AND execution_evidence='not_started'
              AND reconciliation_pending=0
        """, (lease[0], task_id)).rowcount
        if released != 1:
            raise sqlite3.IntegrityError("取消未能封闭并释放原领取")
    elif not db.execute(
        f"SELECT 1 FROM tasks t WHERE t.task_id=? AND {BASE_CANCEL_ELIGIBLE_SQL}", (task_id,)
    ).fetchone():
        return "ineligible", None
    changed = db.execute(
        "UPDATE tasks SET status='cancelled' WHERE task_id=? AND status=?", (task_id, status)
    ).rowcount
    if changed != 1:
        raise sqlite3.IntegrityError("取消未能更新任务")
    return "cancelled", None


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
        fact.state, fact.reason = _decide(db, account, task_id)
        resolved = db.execute(
            """
            UPDATE cancellation_requests SET state=?, reason=?,
                resolved_version=(SELECT snapshot_version + 1 FROM queue_state WHERE id=1)
            WHERE owner_id=? AND task_id=? AND request_id=?
        """,
            (fact.state, fact.reason, *key),
        ).rowcount
        if resolved != 1:
            raise sqlite3.IntegrityError("取消未能记录原请求终局")
    else:
        return
    if db.execute(
        "UPDATE queue_state SET snapshot_version = snapshot_version + 1 WHERE id=1"
    ).rowcount != 1:
        raise sqlite3.IntegrityError("取消未能推进页面版本")


def _advance_once(
    store: "SubmissionStore", account: Account, task_id: str, request_id: str, action: Action
) -> CancellationReceipt | None:
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        if not store.owns_task(db, account, task_id):
            return None
        fact = cancellation_fact(db, account.owner_id, task_id, request_id)
        if action == "decide" and fact.state == "processing":
            db.execute("SAVEPOINT cancel_decision")
            try:
                _transition(db, account, task_id, fact, action)
            except sqlite3.Error as error:
                # 仍持有写锁：先撤回裁决的全部局部写入，再持久隔离该原 ID。
                try:
                    db.execute("ROLLBACK TO cancel_decision")
                    inserted = db.execute("""
                        INSERT OR IGNORE INTO cancellation_recovery_blocks VALUES (?, ?, ?)
                    """, (account.owner_id, task_id, request_id)).rowcount
                    if not db.execute("""
                        SELECT 1 FROM cancellation_recovery_blocks
                        WHERE owner_id=? AND task_id=? AND request_id=?
                    """, (account.owner_id, task_id, request_id)).fetchone():
                        raise sqlite3.IntegrityError("无法证实取消恢复阻断记录")
                    if inserted and db.execute("""
                        UPDATE queue_state SET snapshot_version=snapshot_version+1 WHERE id=1
                    """).rowcount != 1:
                        raise sqlite3.IntegrityError("无法提交取消恢复阻断版本")
                    db.execute("COMMIT")
                except sqlite3.Error:
                    # 全面存储故障不视为成功隔离；进程重启并重新核实前保持关闭。
                    store.cancellation_storage_failed = True
                    raise
                raise CancellationDeferred("取消原请求已隔离，等待服务重试") from error
            db.execute("RELEASE cancel_decision")
            db.execute("""
                DELETE FROM cancellation_recovery_blocks
                WHERE owner_id=? AND task_id=? AND request_id=?
            """, (account.owner_id, task_id, request_id))
        else:
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


def recover_cancellations(store: "SubmissionStore") -> bool:
    with closing(store._connect_readonly()) as db:
        pending = db.execute("""
            SELECT r.owner_id, t.group_id, r.task_id, r.request_id
            FROM cancellation_requests r JOIN tasks t USING (owner_id, task_id)
            WHERE r.state='processing'
        """).fetchall()
    retry = False
    for owner_id, group_id, task_id, request_id in pending:
        try:
            _advance(store, Account(owner_id, group_id), task_id, request_id, "decide")
        except CancellationDeferred:
            retry = True
    return retry
