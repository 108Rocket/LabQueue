import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_submissions import (
    ACCOUNT_PASSWORDS,
    BASE_URL,
    GPU_1,
    GPU_2,
    api_submit,
    committed_admission,
    login,
    payload,
    publish,
)

from labqueue.app import create_app
from labqueue.auth import Account
from labqueue.leases import ClaimBlocked, ClaimCommitUnknown, ClaimedLease
from labqueue.starts import StartBlocked, StartCommitUnknown, StartConfirmed
from labqueue.submissions import SubmissionStore


@pytest.fixture
def database(tmp_path, monkeypatch):
    for account, password in ACCOUNT_PASSWORDS.items():
        monkeypatch.setenv(f"LABQUEUE_{account.upper().replace('-', '_')}_PASSWORD", password)
    return tmp_path / "private" / "dispatch.sqlite3"


@pytest.fixture
def client(database):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        yield client


def history(database):
    with closing(sqlite3.connect(database)) as db:
        return db.execute("SELECT * FROM gpu_leases ORDER BY acquired_version").fetchall()


def owner_task(client, headers, task_id):
    listing = client.get("/api/tasks", headers=headers).json()
    task, = listing["tasks"]
    detail = client.get(f"/api/tasks/{task_id}", headers=headers).json()
    assert detail == {"snapshot_version": listing["snapshot_version"], "task": task}
    return int(listing["snapshot_version"]), task


@pytest.fixture
def admitted_start(client):
    headers = login(client)
    task = api_submit(client, headers)
    observation = publish(client, {GPU_1: "idle", GPU_2: "idle"})
    lease = client.app.state.claim_head()
    assert isinstance(lease, ClaimedLease)
    assert (lease.task_id, lease.gpu_uuid) == (task["task_id"], GPU_1)
    request = dict(task_id=lease.task_id, claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid)
    return headers, observation, request


def start_state(database):
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("BEGIN")
        leases = db.execute("SELECT * FROM gpu_leases ORDER BY acquired_version").fetchall()
        events = db.execute("SELECT * FROM simulated_start_events ORDER BY task_id").fetchall()
        tasks = db.execute("SELECT task_id, status FROM tasks ORDER BY enqueue_seq").fetchall()
        version = db.execute("SELECT snapshot_version FROM queue_state").fetchone()[0]
    return leases, events, tasks, version


@pytest.mark.parametrize("fault", ["committed", "read_unavailable", "not_committed"])
def test_start_commit_fault_and_replay(client, database, admitted_start, monkeypatch, fault):
    headers, observation, request = admitted_start
    executor = Account("demo-a", "group-a")
    start_time = observation.started_at + 1
    client.app.state.observations.advance_clock(start_time)
    before = start_state(database)
    connect = sqlite3.connect
    writers, readonly = [], []

    class LostStartReply(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql != "COMMIT":
                return super().execute(sql, parameters)
            with closing(connect(database)) as observer:
                count = observer.execute("SELECT count(*) FROM simulated_start_events").fetchone()
                assert count == (0,)
                assert observer.execute("SELECT start_confirmed FROM gpu_leases").fetchone() == (0,)
            if fault != "not_committed":
                super().execute(sql, parameters)
            self.close()
            error = sqlite3.OperationalError("synthetic lost start reply")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    def faulting_connect(*args, **kwargs):
        if kwargs.get("uri"):
            assert args[0].endswith("?mode=ro")
            readonly.append(args[0])
            with pytest.raises(sqlite3.ProgrammingError):
                writers[0].execute("SELECT 1")
            if fault == "read_unavailable":
                raise sqlite3.OperationalError("synthetic read failure")
            return connect(*args, **kwargs)
        writer = connect(*args, **kwargs, factory=LostStartReply)
        writers.append(writer)
        return writer

    start = client.app.state.confirm_start
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", faulting_connect)
        if fault == "committed":
            result = start(executor, **request)
        else:
            with pytest.raises(StartCommitUnknown):
                start(executor, **request)
    assert len(writers) == len(readonly) == 1

    if fault == "not_committed":
        assert start_state(database) == before
        return

    expected = StartConfirmed(
        **request, execution_owner_id="demo-a", started_at=start_time,
        snapshot_version=before[3] + 1,
    )
    if fault == "committed":
        assert result == expected
    committed = start_state(database)
    assert committed[0] == [before[0][0][:7] + (1, "started", 0)]
    assert committed[1] == [(
        expected.task_id, expected.claim_id, expected.gpu_uuid, expected.execution_owner_id,
        expected.started_at, expected.snapshot_version,
    )]
    version, task = owner_task(client, headers, expected.task_id)
    assert version == expected.snapshot_version
    assert task["status"] == "running"
    assert task["queue_position"] is None and task["waiting_reason"] is None

    client.app.state.observations.advance_clock(observation.deadline)
    after_expiry = start_state(database)
    for _ in range(2):
        assert start(executor, **request) == expected
    assert start(Account("demo-a-peer", "group-a"), **request) == StartBlocked("identity_mismatch")
    assert start_state(database) == after_expiry
    with closing(connect(database)) as db, db:
        db.execute("""
            UPDATE gpu_leases SET reconciliation_pending = 1, execution_evidence = 'unknown'
            WHERE claim_id = ?
            """, (expected.claim_id,))
    pending = start_state(database)
    assert start(executor, **request) == expected
    assert start_state(database) == pending
    with closing(connect(database)) as db, pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        db.execute("INSERT INTO simulated_start_events SELECT * FROM simulated_start_events")


@pytest.mark.parametrize(("scenario", "reason"), [
    ("expired", "observation_blocked"),
    ("monitor_failed", "observation_blocked"),
    ("visibility_unknown", "observation_blocked"),
    ("old_generation", "claim_invalidated"),
    ("revoked_card", "claim_invalidated"),
    ("released", "claim_mismatch"),
    ("cancelled", "task_not_admitted"),
    ("pending", "reconciliation_pending"),
    ("unknown", "execution_unverified"),
    ("started_without_event", "execution_unverified"),
    ("confirmed_without_event", "execution_unverified"),
    ("wrong_identity", "identity_mismatch"),
    ("wrong_claim", "claim_mismatch"),
    ("wrong_gpu", "claim_mismatch"),
    ("wrong_task", "claim_mismatch"),
])
def test_blocked_start_leaves_committed_state_untouched(
    client, database, admitted_start, scenario, reason
):
    _, observation, request = admitted_start
    executor = Account("demo-a", "group-a")
    if scenario in ("expired", "old_generation"):
        client.app.state.observations.advance_clock(observation.deadline)
        if scenario == "old_generation":
            publish(client, {GPU_1: "idle", GPU_2: "idle"})
    elif scenario == "monitor_failed":
        publish(client, None)
    elif scenario == "visibility_unknown":
        publish(client, {GPU_1: "idle", GPU_2: "unknown"})
    elif scenario == "revoked_card":
        publish(client, {GPU_1: "busy", GPU_2: "idle"})
        publish(client, {GPU_1: "idle", GPU_2: "idle"})
    elif scenario == "wrong_identity":
        executor = Account("demo-a-peer", "group-a")
    elif scenario == "wrong_claim":
        request["claim_id"] = str(uuid4())
    elif scenario == "wrong_gpu":
        request["gpu_uuid"] = GPU_2
    elif scenario == "wrong_task":
        request["task_id"] = str(uuid4())
    else:
        state_fixture = {
            "released": "UPDATE gpu_leases SET released_version = acquired_version + 1",
            "cancelled": "UPDATE tasks SET status = 'cancelled'",
            "pending": "UPDATE gpu_leases SET reconciliation_pending = 1",
            "unknown": "UPDATE gpu_leases SET execution_evidence = 'unknown'",
            "started_without_event": "UPDATE gpu_leases SET execution_evidence = 'started'",
            "confirmed_without_event": "UPDATE gpu_leases SET start_confirmed = 1",
        }
        with closing(sqlite3.connect(database)) as db, db:
            db.execute(state_fixture[scenario])

    before = start_state(database)
    assert before[1] == []
    assert client.app.state.confirm_start(executor, **request) == StartBlocked(reason)
    assert start_state(database) == before


def test_start_rolls_back_confirmation_event_and_version_together(client, database, admitted_start):
    _, _, request = admitted_start
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript("""
            CREATE TRIGGER refuse_running BEFORE UPDATE OF status ON tasks
            WHEN NEW.status = 'running' BEGIN
                SELECT RAISE(ABORT, 'synthetic task write failure');
            END;
            """)
    before = start_state(database)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic task write failure"):
        client.app.state.confirm_start(Account("demo-a", "group-a"), **request)
    assert start_state(database) == before


def test_start_stops_if_durable_evidence_cannot_be_read(
    client, database, admitted_start, monkeypatch
):
    _, _, request = admitted_start
    before = start_state(database)
    connect = SubmissionStore._connect

    def authorize(operation, table, *_):
        if operation == sqlite3.SQLITE_READ and table == "simulated_start_events":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def connect_without_evidence(store):
        db = connect(store)
        db.set_authorizer(authorize)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(SubmissionStore, "_connect", connect_without_evidence)
        with pytest.raises(sqlite3.DatabaseError, match="prohibited"):
            client.app.state.confirm_start(Account("demo-a", "group-a"), **request)
    assert start_state(database) == before


def test_busy_start_rereads_expiry_after_acquiring_write_lock(
    client, database, admitted_start, monkeypatch
):
    _, observation, request = admitted_start
    connect = sqlite3.connect
    writers = []
    expiring = False

    with closing(connect(database, autocommit=True)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")

        class ContendedStart(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                nonlocal expiring
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError as error:
                    assert sql == "BEGIN IMMEDIATE"
                    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                    blocker.execute("ROLLBACK")
                    expiring = True
                    try:
                        client.app.state.observations.advance_clock(observation.deadline)
                    finally:
                        expiring = False
                    raise

        def connect_with_fault(*args, **kwargs):
            if expiring:
                return connect(*args, **kwargs)
            writer = connect(*args, **(kwargs | {"timeout": 0, "factory": ContendedStart}))
            writers.append(writer)
            return writer

        before = start_state(database)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect_with_fault)
            result = client.app.state.confirm_start(Account("demo-a", "group-a"), **request)

    failed, retried = writers
    assert failed is not retried
    assert result == StartBlocked("observation_blocked")
    assert start_state(database) == (*before[:3], before[3] + 1)


def test_start_checks_clock_at_deadline_even_if_expiry_marker_is_missing(
    client, database, admitted_start
):
    _, observation, request = admitted_start
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE gpu_admission_state SET clock = ? WHERE id = 1", (observation.deadline,))
    admission, version = committed_admission(database)
    before = start_state(database)
    assert admission.waiting_reason == "waiting_dispatch"

    result = client.app.state.confirm_start(Account("demo-a", "group-a"), **request)

    expired, expired_version = committed_admission(database)
    assert result == StartBlocked("observation_blocked")
    assert expired.waiting_reason == "gpu_observation_expired" and not expired.candidate_uuids
    assert expired.admission_generation == admission.admission_generation + 1
    assert expired_version == version + 1
    assert start_state(database) == (*before[:3], expired_version)


def test_story_2_2_database_adds_start_events_without_updating_old_claim(
    database, admitted_start
):
    _, _, request = admitted_start
    before = start_state(database)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("DROP TABLE simulated_start_events")

    store = SubmissionStore(database)
    observations = store.initialize()
    assert observations.publish(observations.begin_collection(), {GPU_1: "idle", GPU_2: "idle"})
    assert start_state(database)[:3] == before[:3]
    assert store.confirm_start(Account("demo-a", "group-a"), **request) == StartBlocked(
        "claim_invalidated"
    )
    assert start_state(database)[:3] == before[:3]


def test_two_simulated_cards_start_only_their_matching_tasks(client, database):
    earlier = api_submit(client, login(client))
    later = api_submit(client, login(client, "demo-b"))
    publish(client, {GPU_1: "idle", GPU_2: "idle"})
    claim_one, claim_two = (client.app.state.claim_head() for _ in range(2))
    assert isinstance(claim_one, ClaimedLease) and isinstance(claim_two, ClaimedLease)
    assert (claim_one.task_id, claim_one.gpu_uuid) == (earlier["task_id"], GPU_1)
    assert (claim_two.task_id, claim_two.gpu_uuid) == (later["task_id"], GPU_2)
    start = client.app.state.confirm_start
    before = start_state(database)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first, replay, second = (
            pool.submit(start, account, task_id=claim.task_id,
                        claim_id=claim.claim_id, gpu_uuid=claim.gpu_uuid)
            for claim, account in (
                (claim_one, Account("demo-a", "group-a")),
                (claim_one, Account("demo-a", "group-a")),
                (claim_two, Account("demo-b", "group-b")),
            )
        )
        assert isinstance(first.result(), StartConfirmed)
        assert first.result() == replay.result()
        assert isinstance(second.result(), StartConfirmed)

    leases, events, tasks, version = start_state(database)
    assert {(row[0], row[1], row[2]) for row in events} == {
        (claim_one.task_id, claim_one.claim_id, GPU_1),
        (claim_two.task_id, claim_two.claim_id, GPU_2),
    }
    assert [row[:7] for row in leases] == [row[:7] for row in before[0]]
    assert tasks == [(claim_one.task_id, "running"), (claim_two.task_id, "running")]
    assert version == before[3] + 2


@pytest.mark.parametrize("lose_reply", [False, True], ids=["acknowledged", "lost_after_commit"])
def test_single_available_gpu_keeps_global_fifo_with_concurrent_claims(
    client, database, monkeypatch, lose_reply
):
    headers = login(client)
    body = payload(memory_gib=None)
    earlier = client.post("/api/submissions", json=body, headers=headers).json()
    other = login(client, "demo-b")
    later = api_submit(client, other)
    claim = client.app.state.claim_head
    publish(client, {GPU_1: "busy", GPU_2: "busy"})
    assert claim() == ClaimBlocked("gpu_busy")
    assert history(database) == []

    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    admission, version = committed_admission(database)
    first = SubmissionStore(database)
    second = SubmissionStore(database)
    committing = Event()
    contended = Event()
    resume = Event()
    retry_allowed = Event()
    reply_lost = Event()

    class PausedWriter(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "COMMIT":
                committing.set()
                assert resume.wait(timeout=10)
            cursor = super().execute(sql, parameters)
            if sql == "COMMIT" and lose_reply and not reply_lost.is_set():
                # SQLite 已经提交。关闭写连接，迫使领取者按原 ID 核对回执。
                self.close()
                reply_lost.set()
                retry_allowed.set()
                raise sqlite3.OperationalError("synthetic lost claim reply")
            return cursor

    class ContendingWriter(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as error:
                assert sql == "BEGIN IMMEDIATE"
                assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                contended.set()
                assert retry_allowed.wait(timeout=10)
                raise

    def create_writer(connection_type):
        db = sqlite3.connect(database, timeout=0, autocommit=True, factory=connection_type)
        db.execute("PRAGMA synchronous = FULL")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    monkeypatch.setattr(first, "_connect", lambda: create_writer(PausedWriter))
    monkeypatch.setattr(second, "_connect", lambda: create_writer(ContendingWriter))

    def leading_claim():
        try:
            return first.claim_head()
        finally:
            retry_allowed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        leading = pool.submit(leading_claim)
        try:
            assert committing.wait(timeout=10)
            following = pool.submit(second.claim_head)
            assert contended.wait(timeout=10)
            with closing(sqlite3.connect(database, autocommit=True)) as reader:
                reader.execute("PRAGMA query_only = ON")
                reader.execute("BEGIN")
                assert reader.execute("SELECT snapshot_version FROM queue_state").fetchone() == (
                    version,
                )
                tasks = reader.execute("""
                    SELECT task_id, status FROM tasks ORDER BY enqueue_seq
                    """).fetchall()
                assert tasks == [(earlier["task_id"], "queued"), (later["task_id"], "queued")]
                assert reader.execute("SELECT count(*) FROM gpu_leases").fetchone() == (0,)
        finally:
            resume.set()
        admitted = leading.result(timeout=10)
        assert following.result(timeout=10) == ClaimBlocked("gpu_busy")
    assert reply_lost.is_set() == lose_reply
    assert first.claim_head() == second.claim_head() == ClaimBlocked("gpu_busy")
    assert isinstance(admitted, ClaimedLease)
    assert (admitted.task_id, admitted.gpu_uuid) == (earlier["task_id"], GPU_1)
    assert admitted.admission_generation == admission.admission_generation
    assert admitted.acquired_version == version + 1
    row, = history(database)
    assert row[:6] == (
        admitted.claim_id, earlier["task_id"], GPU_1, admission.admission_generation,
        version + 1, None,
    )

    later_version, task = owner_task(client, other, later["task_id"])
    assert later_version == admitted.acquired_version
    assert task["status"] == "queued"
    assert (task["queue_position"], task["waiting_reason"]) == (1, "gpu_busy")
    denied = client.get(f"/api/tasks/{earlier['task_id']}", headers=other)
    assert denied.status_code == 404 and admitted.claim_id not in denied.text
    assert client.post("/api/dispatch", headers=other).status_code == 404

    headers = login(client)
    owner_version, task = owner_task(client, headers, earlier["task_id"])
    assert owner_version == admitted.acquired_version
    assert task["status"] == "admitted"
    assert (task["queue_position"], task["waiting_reason"]) == (None, None)
    assert "gpu_uuid" not in task and "claim_id" not in task
    receipt_path = f"/api/submissions/{body['submission_key']}"
    assert client.get(receipt_path, headers=headers).json() == earlier
    assert client.post("/api/submissions", json=body, headers=headers).json() == earlier
    assert earlier["memory_gib"] is None

    start = client.app.state.confirm_start
    identity = Account("demo-a", "group-a")
    request = dict(task_id=admitted.task_id, claim_id=admitted.claim_id, gpu_uuid=GPU_1)
    confirmed = start(identity, **request)
    assert isinstance(confirmed, StartConfirmed)
    assert start(identity, **request) == confirmed
    assert start(
        Account("demo-b", "group-b"), **(request | {"task_id": later["task_id"]})
    ) == StartBlocked("claim_mismatch")
    assert claim() == ClaimBlocked("gpu_busy")
    leases, events, tasks, started_version = start_state(database)
    assert [lease[:7] for lease in leases] == [row[:7]]
    assert [event[:3] for event in events] == [(earlier["task_id"], admitted.claim_id, GPU_1)]
    assert tasks == [(earlier["task_id"], "running"), (later["task_id"], "queued")]
    assert started_version == admitted.acquired_version + 1


@pytest.mark.parametrize(
    ("reports", "reason"),
    [
        (None, "gpu_monitoring_failed"),
        ({GPU_1: "idle"}, "gpu_monitoring_failed"),
        ({GPU_1: "idle", GPU_2: "unknown"}, "gpu_visibility_unknown"),
        ("expired", "gpu_observation_expired"),
    ],
)
def test_claim_uses_only_latest_valid_snapshot(client, database, reports, reason):
    headers = login(client)
    task = api_submit(client, headers)
    claim = client.app.state.claim_head
    token = publish(client, {GPU_1: "idle", GPU_2: "busy"})
    if reports == "expired":
        client.app.state.observations.advance_clock(token.deadline)
    else:
        publish(client, reports)
    before = committed_admission(database)
    assert claim() == ClaimBlocked(reason)
    assert history(database) == [] and committed_admission(database) == before
    assert owner_task(client, headers, task["task_id"])[1]["waiting_reason"] == reason

    publish(client, {GPU_1: "busy", GPU_2: "idle"})
    current, version = committed_admission(database)
    assert current.candidate_uuids == (GPU_2,)
    admitted = claim()
    assert isinstance(admitted, ClaimedLease)
    assert (admitted.task_id, admitted.gpu_uuid, admitted.admission_generation) == (
        task["task_id"], GPU_2, current.admission_generation,
    )
    assert admitted.acquired_version == version + 1
    assert claim() == ClaimBlocked("queue_empty")
    saved, = history(database)
    assert saved[:6] == (
        admitted.claim_id, task["task_id"], GPU_2, current.admission_generation,
        version + 1, None,
    )


@pytest.mark.parametrize("invalidated_by", ["external", "generation", "pending"])
def test_earlier_requeue_eligibility_blocks_later_claim(
    client, database, monkeypatch, invalidated_by
):
    earlier = api_submit(client, login(client))
    headers = login(client, "demo-b")
    later = api_submit(client, headers)
    claim = client.app.state.claim_head
    publish(client, {GPU_1: "idle", GPU_2: "idle"})
    first = claim()
    assert isinstance(first, ClaimedLease) and first.gpu_uuid == GPU_1
    admission, version = committed_admission(database)
    if invalidated_by == "external":
        publish(client, {GPU_1: "busy", GPU_2: "idle"})
        revoked, new_version = committed_admission(database)
        assert revoked.admission_generation == admission.admission_generation
        assert new_version == version + 1
        publish(client, {GPU_1: "idle", GPU_2: "idle"})
        assert committed_admission(database)[1] == new_version
    elif invalidated_by == "generation":
        publish(client, None)
        publish(client, {GPU_1: "idle", GPU_2: "idle"})
        current, _ = committed_admission(database)
        assert current.admission_generation > admission.admission_generation
    else:
        # 用持久状态模拟后续 Story 的待核对记录，观测代际保持有效。
        with closing(sqlite3.connect(database, autocommit=True)) as db:
            db.executescript("""
                BEGIN IMMEDIATE;
                UPDATE gpu_leases SET reconciliation_pending = 1;
                UPDATE queue_state SET snapshot_version = snapshot_version + 1;
                COMMIT;
                """)

    retained, = history(database)
    assert retained[5] is None
    assert retained[6] == (invalidated_by == "external")
    assert retained[9] == (invalidated_by == "pending")
    statements = []
    connect = SubmissionStore._connect

    def trace_claim(store):
        db = connect(store)
        db.set_trace_callback(statements.append)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(SubmissionStore, "_connect", trace_claim)
        assert claim() == ClaimBlocked("requeue_required")
    begin, head_read, evidence_read, committed = (
        statements.index("BEGIN IMMEDIATE"),
        next(i for i, sql in enumerate(statements) if "ORDER BY enqueue_seq LIMIT 1" in sql),
        next(i for i, sql in enumerate(statements) if "lease.execution_evidence" in sql),
        statements.index("COMMIT"),
    )
    assert begin < head_read < evidence_read < committed
    assert history(database) == [retained]
    version, waiting = owner_task(client, headers, later["task_id"])
    assert version == committed_admission(database)[1]
    assert (waiting["queue_position"], waiting["waiting_reason"]) == (1, "requeue_required")
    assert earlier["task_id"] not in str(waiting) and first.claim_id not in str(waiting)


@pytest.mark.parametrize(
    ("confirmed", "evidence"), [(0, "unknown"), (0, "started"), (1, "not_started")]
)
def test_uncertain_or_confirmed_execution_keeps_original_card(
    client, database, confirmed, evidence
):
    earlier = api_submit(client, login(client))
    headers = login(client, "demo-b")
    later = api_submit(client, headers)
    claim = client.app.state.claim_head
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    original = claim()
    assert isinstance(original, ClaimedLease)
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("""
            UPDATE gpu_leases SET start_confirmed = ?, execution_evidence = ?,
                reconciliation_pending = 1 WHERE claim_id = ?
            """, (confirmed, evidence, original.claim_id))

    # 换卡前后观测层均有候选卡，页面原因因扣除平台租约而变化。
    before, version = committed_admission(database)
    assert before.waiting_reason == "waiting_dispatch"
    assert owner_task(client, headers, later["task_id"])[1]["waiting_reason"] == "gpu_busy"
    publish(client, {GPU_1: "busy", GPU_2: "idle"})
    after, new_version = committed_admission(database)
    assert after.waiting_reason == before.waiting_reason
    assert new_version == version + 1
    assert owner_task(client, headers, later["task_id"])[1]["waiting_reason"] == "waiting_dispatch"
    second = claim()
    assert isinstance(second, ClaimedLease)
    first, current = history(database)
    assert first[:7] == (
        original.claim_id, earlier["task_id"], GPU_1, original.admission_generation,
        original.acquired_version, None, int(not confirmed),
    )
    assert current[:3] == (second.claim_id, later["task_id"], GPU_2)


def test_legacy_database_migrates_without_changing_queue_or_receipt(database):
    with TestClient(create_app(database), base_url=BASE_URL) as old:
        headers = login(old)
        body = payload(memory_gib=None)
        earlier = old.post("/api/submissions", json=body, headers=headers).json()
        later = api_submit(old, login(old, "demo-b"))
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("DROP TABLE gpu_leases")
        db.execute("UPDATE queue_state SET snapshot_version = 9007199254740993")

    with TestClient(create_app(database), base_url=BASE_URL) as migrated:
        headers = login(migrated)
        assert history(database) == []
        receipt_path = f"/api/submissions/{body['submission_key']}"
        assert migrated.get(receipt_path, headers=headers).json() == earlier
        version, task = owner_task(migrated, headers, earlier["task_id"])
        assert version == 9007199254740993 and task["enqueue_seq"] == "1"
        publish(migrated, {GPU_1: "busy", GPU_2: "idle"})
        admitted = migrated.app.state.claim_head()
        assert isinstance(admitted, ClaimedLease) and admitted.task_id == earlier["task_id"]
        assert admitted.acquired_version == 9007199254740995
        _, task = owner_task(migrated, login(migrated, "demo-b"), later["task_id"])
        assert (task["enqueue_seq"], task["queue_position"]) == ("2", 1)

    with TestClient(create_app(database), base_url=BASE_URL) as restarted:
        saved, = history(database)
        assert saved[:6] == (
            admitted.claim_id, earlier["task_id"], GPU_2, admitted.admission_generation,
            admitted.acquired_version, None,
        )
        assert restarted.app.state.claim_head() == ClaimBlocked("gpu_monitoring_failed")


@pytest.mark.parametrize(
    ("trigger_outcome", "error"),
    [("ABORT, 'synthetic admission failure'", "synthetic admission failure"),
     ("IGNORE", "领取时未能同时写入任务状态和租约")],
)
def test_claim_rollback_keeps_task_lease_and_version_atomic(
    client, database, trigger_outcome, error
):
    headers = login(client)
    task = api_submit(client, headers)
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    before = owner_task(client, headers, task["task_id"])
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute(f"""
            CREATE TRIGGER reject_admission BEFORE UPDATE OF status ON tasks
            WHEN NEW.status = 'admitted'
            BEGIN SELECT RAISE({trigger_outcome}); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match=error):
            client.app.state.claim_head()
        assert history(database) == []
        assert owner_task(client, headers, task["task_id"]) == before
        db.execute("DROP TRIGGER reject_admission")

    admitted = client.app.state.claim_head()
    assert isinstance(admitted, ClaimedLease)
    assert admitted.acquired_version == before[0] + 1


def test_external_revocation_rolls_back_with_observation_and_visible_reason(client, database):
    earlier = api_submit(client, login(client))
    headers = login(client, "demo-b")
    later = api_submit(client, headers)
    publish(client, {GPU_1: "idle", GPU_2: "idle"})
    claim = client.app.state.claim_head()
    assert isinstance(claim, ClaimedLease) and claim.task_id == earlier["task_id"]
    publisher = client.app.state.observations
    token = publisher.begin_collection()
    before = committed_admission(database), history(database)
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("""
            CREATE TRIGGER reject_version BEFORE UPDATE OF snapshot_version ON queue_state
            BEGIN SELECT RAISE(ABORT, 'synthetic version failure'); END
            """)
        with pytest.raises(sqlite3.IntegrityError, match="synthetic version failure"):
            publisher.publish(token, {GPU_1: "busy", GPU_2: "idle"})
        assert (committed_admission(database), history(database)) == before
        _, waiting = owner_task(client, headers, later["task_id"])
        assert waiting["waiting_reason"] == "waiting_dispatch"
        db.execute("DROP TRIGGER reject_version")

    assert publisher.publish(token, {GPU_1: "busy", GPU_2: "idle"})
    assert history(database)[0][5:7] == (None, 1)
    assert client.app.state.claim_head() == ClaimBlocked("requeue_required")


def test_busy_claim_reopens_writer_and_reloads_available_card(client, database, monkeypatch):
    task = api_submit(client, login(client))
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    connect = sqlite3.connect
    starts = []
    publishing = False

    with closing(connect(database, autocommit=True)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")

        class ContendedClaim(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                nonlocal publishing
                if sql == "BEGIN IMMEDIATE" and not publishing:
                    starts.append(self)
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError as error:
                    assert sql == "BEGIN IMMEDIATE"
                    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                    blocker.execute("ROLLBACK")
                    publishing = True
                    try:
                        publish(client, {GPU_1: "busy", GPU_2: "idle"})
                    finally:
                        publishing = False
                    raise

        def connect_with_lock(*args, **kwargs):
            return connect(*args, **(kwargs | {"timeout": 0, "factory": ContendedClaim}))

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect_with_lock)
            admitted = client.app.state.claim_head()

    failed, restarted = starts
    assert failed is not restarted
    with pytest.raises(sqlite3.ProgrammingError):
        failed.execute("SELECT 1")
    assert isinstance(admitted, ClaimedLease)
    assert (admitted.task_id, admitted.gpu_uuid) == (task["task_id"], GPU_2)
    assert len(history(database)) == 1


@pytest.mark.parametrize("fault", ["committed", "other_claim", "read_unavailable"])
def test_uncertain_claim_checks_exact_attempt_in_new_readonly_connection(
    client, database, monkeypatch, fault
):
    earlier = api_submit(client, login(client))
    later = api_submit(client, login(client, "demo-b"))
    publish(client, {GPU_1: "idle", GPU_2: "idle"})
    connect = sqlite3.connect
    failed_writer = None
    readonly = []
    attempted_ids = []
    competing = []

    class LostClaimReply(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "INSERT INTO gpu_leases" in sql:
                attempted_ids.append(parameters[0])
            if sql != "COMMIT":
                return super().execute(sql, parameters)
            if fault == "other_claim":
                self.close()
            else:
                super().execute(sql, parameters)
            if fault != "read_unavailable":
                competing.append(SubmissionStore(database).claim_head())
            error = sqlite3.OperationalError("synthetic lost claim reply")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error

    def faulting_connect(*args, **kwargs):
        nonlocal failed_writer
        if kwargs.get("uri"):
            assert args[0].endswith("?mode=ro")
            with pytest.raises(sqlite3.ProgrammingError):
                failed_writer.execute("SELECT 1")
            readonly.append(args[0])
            if fault == "read_unavailable":
                raise sqlite3.OperationalError("synthetic read failure")
        if failed_writer is None:
            failed_writer = connect(*args, **kwargs, factory=LostClaimReply)
            return failed_writer
        return connect(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", faulting_connect)
        if fault == "committed":
            admitted = client.app.state.claim_head()
            assert isinstance(admitted, ClaimedLease)
            assert admitted.claim_id == attempted_ids[0] and admitted.task_id == earlier["task_id"]
        else:
            with pytest.raises(ClaimCommitUnknown) as unknown:
                client.app.state.claim_head()
            assert unknown.value.claim_id == attempted_ids[0]

    assert len(readonly) == len(attempted_ids) == 1
    rows = history(database)
    assert rows[0][1] == earlier["task_id"]
    if fault == "committed":
        assert len(rows) == 2 and competing[0].task_id == later["task_id"]
        assert rows[0][0] == admitted.claim_id
    elif fault == "other_claim":
        assert len(rows) == 1 and competing[0].task_id == earlier["task_id"]
        assert rows[0][0] == competing[0].claim_id != attempted_ids[0]
    else:
        assert len(rows) == 1 and rows[0][0] == attempted_ids[0]


def test_history_preserves_disjoint_intervals_and_unique_current_leases(client, database):
    earlier = api_submit(client, login(client))
    later = api_submit(client, login(client, "demo-b"))
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    first = client.app.state.claim_head()
    assert isinstance(first, ClaimedLease)

    with closing(sqlite3.connect(database, autocommit=True)) as db:
        duplicate = """
            INSERT INTO gpu_leases (claim_id, task_id, gpu_uuid, admission_generation,
                                    acquired_version) VALUES (?, ?, ?, ?, ?)
            """
        for task_id, gpu_uuid, field in [
            (later["task_id"], GPU_1, "gpu_uuid"), (earlier["task_id"], GPU_2, "task_id"),
        ]:
            with pytest.raises(sqlite3.IntegrityError, match=field):
                db.execute(duplicate, (
                    str(uuid4()), task_id, gpu_uuid, first.admission_generation,
                    first.acquired_version + 1,
                ))
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            db.execute("UPDATE gpu_leases SET released_version = acquired_version")

        # 构造后续 Story 释放后的持久事实，验证历史行不会挡住同卡或同任务的新领取。
        db.execute("BEGIN IMMEDIATE")
        released = db.execute("""
            UPDATE queue_state SET snapshot_version = snapshot_version + 1
            WHERE id = 1 RETURNING snapshot_version
            """).fetchone()[0]
        db.execute("UPDATE gpu_leases SET released_version = ? WHERE claim_id = ?", (
            released, first.claim_id,
        ))
        db.execute("UPDATE tasks SET status = 'queued' WHERE task_id = ?", (earlier["task_id"],))
        db.execute("COMMIT")

        second = client.app.state.claim_head()
        assert isinstance(second, ClaimedLease)
        assert second.claim_id != first.claim_id
        assert (second.gpu_uuid, second.task_id) == (first.gpu_uuid, first.task_id)
        overlaps = db.execute("""
            SELECT count(*) FROM gpu_leases AS first JOIN gpu_leases AS second
              ON (first.gpu_uuid = second.gpu_uuid OR first.task_id = second.task_id)
             AND first.acquired_version < second.acquired_version
            WHERE first.released_version IS NULL OR first.released_version > second.acquired_version
            """).fetchone()[0]
        assert overlaps == 0
    previous, current = history(database)
    assert previous[4:6] == (first.acquired_version, released)
    assert current[4:6] == (released + 1, None)
