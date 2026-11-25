"""双卡模拟观测及其持久准入状态。"""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import uuid4

SIMULATED_GPU_UUIDS = (
    "GPU-6c61338a-4bc7-4a97-a6a4-293caa9f8601",
    "GPU-6c61338a-4bc7-4a97-a6a4-293caa9f8602",
)
OBSERVATION_TTL_SECONDS = 30
MAX_CLOCK_SECONDS = 2**63 - 1 - OBSERVATION_TTL_SECONDS

Occupancy = Literal["idle", "busy", "unknown"]
WaitingReason = Literal[
    "gpu_monitoring_failed",
    "gpu_observation_expired",
    "gpu_visibility_unknown",
    "gpu_busy",
    "waiting_dispatch",
]


@dataclass(frozen=True, slots=True)
class CollectionToken:
    publisher_generation: int
    sequence: int
    started_at: int
    deadline: int
    signature: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class AdmissionState:
    clock: int
    publisher_generation: int
    publisher_id: str
    admission_generation: int
    issued_sequence: int
    published_sequence: int
    deadline: int | None
    waiting_reason: WaitingReason
    candidate_uuids: tuple[str, ...]


class StalePublisher(Exception):
    """采集发布者已被替换。"""


def read_admission(db: sqlite3.Connection) -> AdmissionState:
    """在调用方事务中读取状态，供本人查询及后续准入裁决使用。"""
    if not db.in_transaction:
        raise RuntimeError("读取模拟准入状态需要调用方事务")
    with closing(db.execute("SELECT * FROM gpu_admission_state WHERE id = 1")) as cursor:
        cursor.row_factory = sqlite3.Row
        row = cursor.fetchone()
    return AdmissionState(
        clock=row["clock"],
        publisher_generation=row["publisher_generation"],
        publisher_id=row["publisher_id"],
        admission_generation=row["admission_generation"],
        issued_sequence=row["issued_sequence"],
        published_sequence=row["published_sequence"],
        deadline=row["deadline"],
        waiting_reason=row["waiting_reason"],
        candidate_uuids=tuple(
            gpu_uuid for gpu_uuid, available in zip(
                SIMULATED_GPU_UUIDS, (row["gpu_1_available"], row["gpu_2_available"]), strict=True
            ) if available
        ),
    )


def _advance_snapshot_version(db: sqlite3.Connection) -> None:
    db.execute("UPDATE queue_state SET snapshot_version = snapshot_version + 1 WHERE id = 1")


def _save(db: sqlite3.Connection, before: AdmissionState, after: AdmissionState) -> None:
    if before == after:
        return
    gpu_1, gpu_2 = SIMULATED_GPU_UUIDS
    db.execute(
        """UPDATE gpu_admission_state
           SET clock = ?, publisher_generation = ?, publisher_id = ?, admission_generation = ?,
               issued_sequence = ?, published_sequence = ?, deadline = ?, waiting_reason = ?,
               gpu_1_available = ?, gpu_2_available = ?
           WHERE id = 1""",
        (
            after.clock, after.publisher_generation, after.publisher_id, after.admission_generation,
            after.issued_sequence, after.published_sequence, after.deadline, after.waiting_reason,
            gpu_1 in after.candidate_uuids, gpu_2 in after.candidate_uuids,
        ),
    )
    if before.waiting_reason != after.waiting_reason:
        _advance_snapshot_version(db)


def _rotate_publisher(db: sqlite3.Connection, before: AdmissionState, publisher_id: str) -> int:
    after = replace(
        before,
        publisher_generation=before.publisher_generation + 1,
        publisher_id=publisher_id,
        admission_generation=before.admission_generation + 1,
        published_sequence=0,
        deadline=None,
        waiting_reason="gpu_monitoring_failed",
        candidate_uuids=(),
    )
    _save(db, before, after)
    return after.publisher_generation


def initialize_observations(db: sqlite3.Connection) -> tuple[int, str]:
    """在启动写事务中迁移并封闭上次运行的观测。"""
    db.execute("""
        CREATE TABLE IF NOT EXISTS gpu_admission_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            clock INTEGER NOT NULL,
            publisher_generation INTEGER NOT NULL,
            publisher_id TEXT NOT NULL,
            admission_generation INTEGER NOT NULL,
            issued_sequence INTEGER NOT NULL,
            published_sequence INTEGER NOT NULL,
            deadline INTEGER,
            waiting_reason TEXT NOT NULL,
            gpu_1_available INTEGER NOT NULL CHECK (gpu_1_available IN (0, 1)),
            gpu_2_available INTEGER NOT NULL CHECK (gpu_2_available IN (0, 1))
        )
        """)
    created = db.execute("""
        INSERT OR IGNORE INTO gpu_admission_state
        VALUES (1, 0, 0, '', 0, 0, 0, NULL, 'gpu_monitoring_failed', 0, 0)
        """).rowcount
    if created and db.execute("""
        SELECT 1 FROM sqlite_schema
        WHERE type = 'table' AND name = 'simulated_gpu_observations'
        """).fetchone():
        # 旧库已发布过页面等待原因，迁移后需推进同一个版本域。
        _advance_snapshot_version(db)
    publisher_id = uuid4().hex
    return _rotate_publisher(db, read_admission(db), publisher_id), publisher_id


def _valid_observation(state: AdmissionState) -> bool:
    return state.waiting_reason in ("gpu_busy", "waiting_dispatch")


def _expire(state: AdmissionState) -> AdmissionState:
    if _valid_observation(state) and state.deadline is not None and state.clock >= state.deadline:
        return replace(
            state,
            admission_generation=state.admission_generation + 1,
            waiting_reason="gpu_observation_expired",
            candidate_uuids=(),
        )
    return state


def _sample_result(
    clock: int, token: CollectionToken, reports: Mapping[str, Occupancy] | None
) -> tuple[WaitingReason, tuple[str, ...]]:
    if reports is None or reports.keys() != set(SIMULATED_GPU_UUIDS):
        return "gpu_monitoring_failed", ()
    occupancy = tuple(reports[gpu_uuid] for gpu_uuid in SIMULATED_GPU_UUIDS)
    if any(value not in ("idle", "busy", "unknown") for value in occupancy):
        return "gpu_monitoring_failed", ()
    if "unknown" in occupancy:
        return "gpu_visibility_unknown", ()
    if clock >= token.deadline:
        return "gpu_observation_expired", ()
    candidates = tuple(
        gpu_uuid for gpu_uuid, value in zip(SIMULATED_GPU_UUIDS, occupancy, strict=True)
        if value == "idle"
    )
    return ("waiting_dispatch" if candidates else "gpu_busy"), candidates


class SimulatedObservations:
    """受信任的本地模拟入口，发布者身份随实例固定。"""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        publisher_generation: int,
        publisher_id: str,
    ) -> None:
        self._connect = connect
        self._publisher_generation = publisher_generation
        self._publisher_id = publisher_id
        self._token_key = secrets.token_bytes(32)

    def _open_writer(self) -> sqlite3.Connection:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
        except BaseException:
            db.close()
            raise
        return db

    @contextmanager
    def _write(
        self, commit: Callable[[sqlite3.Connection], None] | None = None
    ) -> Iterator[sqlite3.Connection]:
        try:
            db = self._open_writer()
        except sqlite3.OperationalError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF != sqlite3.SQLITE_BUSY:
                raise
            db = self._open_writer()
        with closing(db):
            try:
                yield db
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            (commit or self._commit)(db)

    @staticmethod
    def _commit(
        db: sqlite3.Connection, reconcile: Callable[[], bool] | None = None
    ) -> None:
        try:
            db.execute("COMMIT")
        except sqlite3.Error:
            if db.in_transaction:
                db.execute("ROLLBACK")
                raise
            if reconcile is None:
                raise
            db.close()
            if not reconcile():
                raise

    def commit_rotation(self, db: sqlite3.Connection) -> None:
        """回执不明时只从新连接核对这次换代的持久身份。"""
        self._commit(db, self._is_committed)

    def _is_committed(self) -> bool:
        with closing(self._connect()) as db:
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
            return self._is_current(read_admission(db))

    def _is_current(self, state: AdmissionState) -> bool:
        return (
            state.publisher_generation == self._publisher_generation
            and state.publisher_id == self._publisher_id
        )

    def _require_current(self, state: AdmissionState) -> None:
        if not self._is_current(state):
            raise StalePublisher

    def replace_publisher(self) -> SimulatedObservations:
        publisher_id = uuid4().hex
        replacement = SimulatedObservations(
            self._connect, self._publisher_generation + 1, publisher_id
        )
        with self._write(commit=replacement.commit_rotation) as db:
            before = read_admission(db)
            self._require_current(before)
            _rotate_publisher(db, before, publisher_id)
        return replacement

    def _sign_collection(self, sequence: int, started_at: int, deadline: int) -> bytes:
        fields = (
            f"{self._publisher_id}:{self._publisher_generation}:{sequence}:{started_at}:{deadline}"
        )
        return hmac.digest(self._token_key, fields.encode("ascii"), "sha256")

    def begin_collection(self) -> CollectionToken:
        with self._write() as db:
            before = read_admission(db)
            self._require_current(before)
            sequence = before.issued_sequence + 1
            deadline = before.clock + OBSERVATION_TTL_SECONDS
            if before.clock > MAX_CLOCK_SECONDS:
                raise ValueError("模拟时钟无法记录下一次采集的截止点")
            _save(db, before, replace(before, issued_sequence=sequence))
        signature = self._sign_collection(sequence, before.clock, deadline)
        return CollectionToken(
            self._publisher_generation, sequence, before.clock, deadline, signature
        )

    def advance_clock(self, seconds: int) -> None:
        """推进到给定的模拟时间，和已到期状态一起提交。"""
        if type(seconds) is not int or not 0 <= seconds <= MAX_CLOCK_SECONDS:
            raise ValueError("模拟时钟必须为非负秒数，并为采集截止点保留 SQLite 整数范围")
        with self._write() as db:
            before = read_admission(db)
            self._require_current(before)
            _save(db, before, _expire(replace(before, clock=max(before.clock, seconds))))

    def publish(self, token: CollectionToken, reports: Mapping[str, Occupancy] | None) -> bool:
        """提交采集结果。查询失败使用 None，拒收的令牌返回 False。"""
        # 先在事务外复制探测结果，写事务只处理本地数据。
        reports = dict(reports) if reports is not None else None
        with self._write() as db:
            before = read_admission(db)
            current = _expire(before)
            if (
                not self._is_current(current)
                or token.publisher_generation != current.publisher_generation
                or not current.published_sequence < token.sequence <= current.issued_sequence
                or not hmac.compare_digest(
                    token.signature,
                    self._sign_collection(token.sequence, token.started_at, token.deadline),
                )
            ):
                _save(db, before, current)
                return False
            reason, candidates = _sample_result(current.clock, token, reports)
            entering_block = _valid_observation(current) and reason not in (
                "gpu_busy", "waiting_dispatch"
            )
            after = replace(
                current,
                admission_generation=current.admission_generation + entering_block,
                published_sequence=token.sequence,
                deadline=token.deadline,
                waiting_reason=reason,
                candidate_uuids=candidates,
            )
            _save(db, before, after)
        return True
