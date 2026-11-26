"""任务提交、模拟领取与启动、幂等重放和只读核对。"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import UUID4, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from labqueue.auth import Account
from labqueue.cancellations import (
    CANCEL_ELIGIBLE_SQL,
    CancellationFact,
    initialize_cancellations,
)
from labqueue.leases import (
    ClaimBlocked,
    ClaimCommitUnknown,
    ClaimedLease,
    ClaimResult,
    dispatch_options,
    initialize_leases,
    read_claim,
    record_claim,
)
from labqueue.observations import (
    SimulatedObservations,
    WaitingReason,
    initialize_observations,
    read_admission,
    read_admission_for_dispatch,
)
from labqueue.results import (
    EndBlocked,
    EndCommitUnknown,
    EndConfirmed,
    EndReport,
    decide_end,
    initialize_results,
    read_result,
)
from labqueue.starts import (
    StartCommitUnknown,
    StartConfirmed,
    StartResult,
    decide_start,
    initialize_starts,
    read_start,
)
from labqueue.storage import prepare_storage, rollback_open_transaction, verify_storage

LOCK_WAIT_SECONDS = 2


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_key: UUID4
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    command: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
    memory_gib: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None = None

    @field_validator("name", "command")
    @classmethod
    def require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入内容")
        return value


class Receipt(BaseModel):
    task_id: UUID
    enqueue_seq: int
    queued_at: datetime
    memory_gib: Decimal | None
    status: Literal["queued"] = "queued"


class TaskView(BaseModel):
    task_id: UUID
    name: str
    enqueue_seq: str
    queued_at: datetime
    status: str
    queue_position: int | None
    waiting_reason: WaitingReason | Literal["requeue_required"] | None
    failure_reason: Literal["oom"] | None = Field(default=None, exclude_if=lambda v: v is None)
    can_cancel: bool
    cancellation: CancellationFact | None = Field(default=None, exclude_if=lambda v: v is None)


class TaskListSnapshot(BaseModel):
    snapshot_version: str
    tasks: list[TaskView]


class TaskDetailSnapshot(BaseModel):
    snapshot_version: str
    task: TaskView
    output_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)


class TaskOutput(BaseModel):
    text: str


class SubmissionConflict(Exception):
    """该账号和提交键已用于不同内容。"""


class CommitUncertain(Exception):
    """需要关闭原连接并从已提交快照核对写入。"""


class SubmissionStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=LOCK_WAIT_SECONDS, autocommit=True)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous = FULL")
            db.execute("PRAGMA foreign_keys = ON")
            if db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise RuntimeError("SQLite 未启用 synchronous=FULL")
        except BaseException:
            db.close()
            raise
        return db

    def _connect_readonly(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.path.absolute().as_uri() + "?mode=ro",
            uri=True,
            timeout=LOCK_WAIT_SECONDS,
            autocommit=True,
        )

    def initialize(self) -> SimulatedObservations:
        prepare_storage(self.path)
        with closing(self._connect()) as db:
            if db.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise RuntimeError("SQLite 未启用 WAL")
            # 读事务会实际打开 WAL 与 SHM；持有连接时核验，再写业务数据。
            db.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            verify_storage(self.path)
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS queue_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enqueue_seq INTEGER NOT NULL,
                    snapshot_version INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO queue_state VALUES (1, 0, 0);
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY NOT NULL,
                    owner_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    submission_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    memory_gib TEXT,
                    status TEXT NOT NULL,
                    enqueue_seq INTEGER NOT NULL UNIQUE,
                    queued_at TEXT NOT NULL,
                    UNIQUE (owner_id, submission_key)
                );
                CREATE INDEX IF NOT EXISTS queued_by_sequence ON tasks (enqueue_seq)
                    WHERE status = 'queued';
                CREATE TABLE IF NOT EXISTS task_outputs (
                    output_id TEXT PRIMARY KEY NOT NULL,
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks (task_id),
                    text TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS task_output_inserted
                AFTER INSERT ON task_outputs BEGIN
                    UPDATE queue_state SET snapshot_version = snapshot_version + 1 WHERE id = 1;
                END;
                CREATE TRIGGER IF NOT EXISTS task_output_updated
                AFTER UPDATE ON task_outputs BEGIN
                    UPDATE queue_state SET snapshot_version = snapshot_version + 1 WHERE id = 1;
                END;
                CREATE TRIGGER IF NOT EXISTS task_output_deleted
                AFTER DELETE ON task_outputs BEGIN
                    UPDATE queue_state SET snapshot_version = snapshot_version + 1 WHERE id = 1;
                END;
                """)
            initialize_leases(db)
            initialize_starts(db)
            initialize_results(db)
            initialize_cancellations(db)
            publisher = SimulatedObservations(
                self._connect, self._connect_readonly, *initialize_observations(db)
            )
            publisher.commit_rotation(db)
        return publisher

    def claim_head(self) -> ClaimResult:
        """仅供应用进程内受信任的模拟调用。"""
        try:
            return self._claim_head()
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY:
                raise
            return self._claim_head()

    def _claim_head(self) -> ClaimResult:
        result: ClaimResult
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                admission = read_admission_for_dispatch(db)
                options = dispatch_options(db, admission)
                if options.head_task_id is None:
                    result = ClaimBlocked("queue_empty")
                elif options.gpu_uuid is None:
                    result = ClaimBlocked(options.waiting_reason)
                else:
                    result = record_claim(
                        db, options.head_task_id, options.gpu_uuid, admission.admission_generation
                    )
            except BaseException:
                rollback_open_transaction(db)
                raise

            try:
                db.execute("COMMIT")
            except sqlite3.Error as error:
                if rollback_open_transaction(db):
                    raise
                commit_error = error
            else:
                return result

        claim_id = result.claim_id if isinstance(result, ClaimedLease) else None
        if claim_id is not None:
            try:
                with closing(self._connect_readonly()) as db:
                    committed = read_claim(db, claim_id)
            except (sqlite3.Error, OSError) as error:
                raise ClaimCommitUnknown(claim_id) from error
            if committed is not None and committed == result:
                return committed
        raise ClaimCommitUnknown(claim_id) from commit_error

    def confirm_start(
        self, executor: Account, *, task_id: str, claim_id: str, gpu_uuid: str
    ) -> StartResult:
        """仅供应用进程内受信任的本地模拟执行方调用。"""
        try:
            return self._confirm_start(executor, task_id, claim_id, gpu_uuid)
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY:
                raise
            return self._confirm_start(executor, task_id, claim_id, gpu_uuid)

    def _confirm_start(
        self, executor: Account, task_id: str, claim_id: str, gpu_uuid: str
    ) -> StartResult:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = decide_start(db, executor, task_id, claim_id, gpu_uuid)
            except BaseException:
                rollback_open_transaction(db)
                raise

            try:
                db.execute("COMMIT")
            except sqlite3.Error as error:
                if rollback_open_transaction(db):
                    raise
                commit_error = error
            else:
                return result

        if isinstance(result, StartConfirmed):
            try:
                with closing(self._connect_readonly()) as db:
                    committed = read_start(db, task_id)
            except (sqlite3.Error, OSError) as error:
                raise StartCommitUnknown("无法读取模拟启动确认") from error
            if committed is not None and committed == result:
                return committed
        raise StartCommitUnknown("无法证实模拟启动已提交") from commit_error

    def report_end(self, executor: Account, report: EndReport) -> EndConfirmed | EndBlocked:
        """由受信任的本地模拟执行方报告结束证据。"""
        try:
            return self._report_end(executor, report)
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY:
                raise
            return self._report_end(executor, report)

    def _report_end(self, executor: Account, report: EndReport) -> EndConfirmed | EndBlocked:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                result = decide_end(db, executor, report)
            except BaseException:
                rollback_open_transaction(db)
                raise
            try:
                db.execute("COMMIT")
            except sqlite3.Error as error:
                if rollback_open_transaction(db):
                    raise
                commit_error = error
            else:
                return result

        if isinstance(result, EndConfirmed):
            try:
                with closing(self._connect_readonly()) as db:
                    committed = read_result(db, report.task_id)
            except (sqlite3.Error, OSError) as error:
                raise EndCommitUnknown("无法读取模拟终态") from error
            if committed == result:
                return result
        raise EndCommitUnknown("无法证实模拟终态已提交") from commit_error

    def list_tasks(
        self, account: Account, pending: dict[str, str] | None = None
    ) -> TaskListSnapshot:
        version, tasks, _ = self._task_snapshot(account, None, pending or {})
        return TaskListSnapshot(snapshot_version=version, tasks=tasks)

    def task_detail(
        self, account: Account, task_id: str, request_id: str | None = None
    ) -> tuple[str, TaskDetailSnapshot | None]:
        pending = {task_id: request_id} if request_id else {}
        version, tasks, output_id = self._task_snapshot(account, task_id, pending)
        if not tasks:
            return version, None
        return version, TaskDetailSnapshot(
            snapshot_version=version, task=tasks[0], output_id=output_id
        )

    @staticmethod
    def owns_task(db: sqlite3.Connection, account: Account, task_id: str) -> bool:
        """在调用者持有的事务内检查可信所有者。"""
        return (
            db.execute(
                "SELECT 1 FROM tasks WHERE task_id = ? AND owner_id = ?",
                (task_id, account.owner_id),
            ).fetchone()
            is not None
        )

    def read_task_output(self, account: Account, task_id: str, output_id: str) -> TaskOutput | None:
        with closing(self._connect()) as db:
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            if not self.owns_task(db, account, task_id):
                return None
            row = db.execute(
                "SELECT text FROM task_outputs WHERE task_id = ? AND output_id = ?",
                (task_id, output_id),
            ).fetchone()
        return TaskOutput(text=row["text"]) if row is not None else None

    def _task_snapshot(
        self, account: Account, task_id: str | None, pending: dict[str, str]
    ) -> tuple[str, list[TaskView], str | None]:
        output_id = None
        with closing(self._connect()) as db:
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            # 首次读取固定 WAL 快照，后续读取沿用同一已提交版本。
            version = db.execute(
                "SELECT snapshot_version FROM queue_state WHERE id = 1"
            ).fetchone()[0]
            reason = dispatch_options(db, read_admission(db)).waiting_reason
            if task_id is None:
                rows = db.execute(
                    f"""WITH ranked AS (
                           SELECT task_id,
                                  row_number() OVER (ORDER BY enqueue_seq) AS queue_position
                           FROM tasks WHERE status = 'queued'
                       )
                       SELECT t.task_id, t.name, t.enqueue_seq, t.queued_at, t.status,
                              ranked.queue_position, r.outcome,
                              ({CANCEL_ELIGIBLE_SQL}) AS can_cancel
                       FROM tasks AS t LEFT JOIN ranked ON ranked.task_id = t.task_id
                       LEFT JOIN simulated_results r ON r.task_id = t.task_id
                       WHERE t.owner_id = ? ORDER BY t.enqueue_seq""",
                    (account.owner_id,),
                ).fetchall()
            elif self.owns_task(db, account, task_id):
                rows = db.execute(
                    f"""SELECT t.task_id, t.name, t.enqueue_seq, t.queued_at, t.status,
                              output.output_id, r.outcome, ({CANCEL_ELIGIBLE_SQL}) AS can_cancel,
                              CASE WHEN t.status = 'queued' THEN (
                                  SELECT count(*) + 1 FROM tasks AS earlier
                                  WHERE earlier.status = 'queued'
                                    AND earlier.enqueue_seq < t.enqueue_seq
                              ) END AS queue_position
                       FROM tasks AS t LEFT JOIN task_outputs AS output USING (task_id)
                       LEFT JOIN simulated_results r USING (task_id)
                       WHERE t.task_id = ?""",
                    (task_id,),
                ).fetchall()
                output_id = rows[0]["output_id"]
            else:
                rows = []
            facts = {}
            if pending and rows:
                for key, request_id, state in db.execute("""
                    SELECT p.key, p.value, coalesce(r.state, 'unknown')
                    FROM json_each(?) p LEFT JOIN cancellation_requests r
                      ON r.owner_id=? AND r.task_id=p.key AND r.request_id=p.value
                """, (json.dumps(pending), account.owner_id)):
                    facts[key] = CancellationFact(request_id=request_id, state=state)
            tasks = [
                TaskView(
                    task_id=row["task_id"],
                    name=row["name"],
                    enqueue_seq=str(row["enqueue_seq"]),
                    queued_at=row["queued_at"],
                    status=row["status"],
                    failure_reason="oom" if row["outcome"] == "oom" else None,
                    queue_position=row["queue_position"],
                    waiting_reason=reason if row["status"] == "queued" else None,
                    can_cancel=row["can_cancel"],
                    cancellation=facts.get(row["task_id"]),
                )
                for row in rows
            ]
        return str(version), tasks, output_id

    @staticmethod
    def _find(db: sqlite3.Connection, owner_id: str, key: UUID) -> sqlite3.Row | None:
        return db.execute(
            """SELECT task_id, enqueue_seq, queued_at, memory_gib, name, command
               FROM tasks WHERE owner_id = ? AND submission_key = ?""",
            (owner_id, str(key)),
        ).fetchone()

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Receipt:
        return Receipt.model_validate(dict(row))

    def lookup(self, account: Account, key: UUID) -> Receipt | None:
        with closing(self._connect()) as db:
            db.execute("PRAGMA query_only = ON")
            row = self._find(db, account.owner_id, key)
        return self._receipt(row) if row is not None else None

    def _replay(self, row: sqlite3.Row, submission: Submission) -> Receipt:
        receipt = self._receipt(row)
        if (row["name"], row["command"], receipt.memory_gib) != (
            submission.name,
            submission.command,
            submission.memory_gib,
        ):
            raise SubmissionConflict
        return receipt

    def submit(self, account: Account, submission: Submission) -> Receipt:
        try:
            try:
                return self._write(account, submission)
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode & 0xFF != sqlite3.SQLITE_BUSY:
                    raise
                # 原连接已关闭，从新的写事务重查提交键。
                return self._write(account, submission)
        except CommitUncertain:
            with closing(self._connect()) as db:
                db.execute("PRAGMA query_only = ON")
                row = self._find(db, account.owner_id, submission.submission_key)
            if row is None:
                raise
            return self._replay(row, submission)

    def _write(self, account: Account, submission: Submission) -> Receipt:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._find(db, account.owner_id, submission.submission_key)
            if row is not None:
                db.execute("ROLLBACK")
                return self._replay(row, submission)

            enqueue_seq = db.execute("""
                UPDATE queue_state
                SET enqueue_seq = enqueue_seq + 1, snapshot_version = snapshot_version + 1
                WHERE id = 1 RETURNING enqueue_seq
                """).fetchone()[0]
            receipt = Receipt(
                task_id=uuid4(),
                enqueue_seq=enqueue_seq,
                queued_at=datetime.now(UTC),
                memory_gib=submission.memory_gib,
            )
            db.execute(
                """INSERT INTO tasks (
                    task_id, owner_id, group_id, submission_key, name, command, memory_gib,
                    status, enqueue_seq, queued_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(receipt.task_id),
                    account.owner_id,
                    account.group_id,
                    str(submission.submission_key),
                    submission.name,
                    submission.command,
                    str(receipt.memory_gib) if receipt.memory_gib is not None else None,
                    receipt.status,
                    receipt.enqueue_seq,
                    receipt.queued_at.isoformat(),
                ),
            )
            try:
                db.execute("COMMIT")
            except sqlite3.Error as error:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                    raise
                raise CommitUncertain from error
        return receipt
