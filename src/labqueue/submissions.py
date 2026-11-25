"""任务提交、幂等重放和只读核对。"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import UUID4, BaseModel, ConfigDict, Field, StringConstraints, field_validator

from labqueue.auth import Account
from labqueue.storage import prepare_storage, verify_storage

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
            if db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise RuntimeError("SQLite 未启用 synchronous=FULL")
        except BaseException:
            db.close()
            raise
        return db

    def initialize(self) -> None:
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
                COMMIT;
                """)

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
