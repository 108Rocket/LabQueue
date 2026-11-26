"""Story 3.1 的 HTTP 命令、已提交历史及重启核对。"""

import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import ExitStack, closing
from threading import Event, local
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_submissions import ACCOUNT_PASSWORDS, BASE_URL, GPU_1, GPU_2, api_submit, login, publish

import labqueue.cancellations as cancellations
from labqueue.app import create_app
from labqueue.auth import Account
from labqueue.submissions import SubmissionStore


@pytest.fixture
def database(tmp_path, monkeypatch):
    for account, password in ACCOUNT_PASSWORDS.items():
        monkeypatch.setenv(f"LABQUEUE_{account.upper().replace('-', '_')}_PASSWORD", password)
    return tmp_path / "private" / "cancel.sqlite3"


def history(path):
    tables = (
        "tasks",
        "queue_state",
        "gpu_leases",
        "simulated_start_events",
        "simulated_results",
        "cancellation_requests",
        "cancellation_recovery_blocks",
    )
    with closing(sqlite3.connect(path)) as observer:
        observer.execute("BEGIN")
        return {t: observer.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}



def assert_view(client, owner, task, status, eligible):
    detail = client.get(f"/api/tasks/{task}", headers=owner).json()["task"]
    listing = client.get("/api/tasks", headers=owner).json()["tasks"]
    assert detail == next(row for row in listing if row["task_id"] == task)
    assert (detail["status"], detail["can_cancel"]) == (status, eligible)


def command(task, key):
    return f"/api/tasks/{task}/cancellations/{key}"


def test_cancel_once_moves_next_task_and_only_next_can_claim(database):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        publish(client, {GPU_1: "busy", GPU_2: "busy"})
        a, b = [api_submit(client, owner)["task_id"] for _ in range(2)]
        queued = client.get("/api/tasks", headers=owner).json()["tasks"]
        assert [(t["queue_position"], t["can_cancel"]) for t in queued] == [(1, True), (2, True)]
        key = str(uuid4())
        path = command(a, key)
        receipt = client.post(path, headers=owner).json()
        assert receipt["state"] == "cancelled"
        before = history(database)
        assert before["gpu_leases"] == before["simulated_start_events"] == []
        assert before["cancellation_requests"] == [
            (
                "demo-a",
                a,
                key,
                "cancelled",
                int(receipt["snapshot_version"]) - 1,
                int(receipt["snapshot_version"]),
                None,
            )
        ]
        for suffix in ("", "/seal"):
            assert client.post(path + suffix, headers=owner).json() == receipt
            assert history(database) == before

        listing = client.get("/api/tasks", params={"pending": f"{a}.{key}"}, headers=owner).json()
        first, second = listing["tasks"]
        assert first["cancellation"] == {"request_id": key, "state": "cancelled"}
        assert first["status"] == "cancelled" and first["queue_position"] is None
        assert not first["can_cancel"] and second["queue_position"] == 1
        assert second["enqueue_seq"] == queued[1]["enqueue_seq"]
        assert client.get(f"/api/tasks/{a}?request_id={key}", headers=owner).json() == {
            "task": first, "snapshot_version": listing["snapshot_version"],
        }
        assert client.get(f"/api/tasks/{b}", headers=owner).json() == {
            "task": second, "snapshot_version": listing["snapshot_version"],
        }
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        lease = client.app.state.claim_head()
        assert (lease.task_id, lease.gpu_uuid) == (b, GPU_1)
        facts = history(database)
        assert [row[1] for row in facts["gpu_leases"]] == [b]
        assert facts["simulated_start_events"] == []
        assert facts["cancellation_requests"] == before["cancellation_requests"]
        assert client.get(f"/api/tasks/{a}", headers=owner).json()["task"]["status"] == "cancelled"


@pytest.mark.parametrize("admitted", [False, True])
def test_unknown_refresh_then_seal_survives_restart_and_late_request(database, admitted):
    key = str(uuid4())
    held, destination = Event(), Future()

    def deliver_late(path):
        # 传输端扣住命令直到新服务就绪，目标进程使用本人重新登录的会话。
        held.set()
        client, owner = destination.result(10)
        return client.post(path, headers=owner)

    with ThreadPoolExecutor(max_workers=1) as workers:
        with TestClient(create_app(database), base_url=BASE_URL) as client:
            owner = login(client)
            task = api_submit(client, owner)["task_id"]
            if admitted:
                publish(client, {GPU_1: "idle", GPU_2: "busy"})
                assert client.app.state.claim_head().task_id == task
            assert_view(client, owner, task, "admitted" if admitted else "queued", True)
            path = command(task, key)
            delayed = workers.submit(deliver_late, path)
            assert held.wait(10)
            before = history(database)
            for _ in range(2):
                detail = client.get(f"/api/tasks/{task}?request_id={key}", headers=owner).json()
                assert detail["task"]["cancellation"] == {"request_id": key, "state": "unknown"}
            assert history(database) == before
            assert client.post(path + "/seal", headers=owner).json()["state"] == "sealed"
            assert_view(client, owner, task, "admitted" if admitted else "queued", True)

        with TestClient(create_app(database), base_url=BASE_URL) as client:
            owner = login(client)
            before = history(database)
            destination.set_result((client, owner))
            assert delayed.result(10).json()["state"] == "sealed"
            for suffix in ("", "/seal"):
                assert client.post(path + suffix, headers=owner).json()["state"] == "sealed"
            assert history(database) == before
            assert client.post(command(task, uuid4()), headers=owner).json()["state"] == "cancelled"


@pytest.mark.parametrize("claim_first", [False, True])
def test_accepted_request_recovered_using_current_evidence(database, monkeypatch, claim_first):
    key = str(uuid4())
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        path = command(task, key)
        advance = cancellations._advance

        def interrupt_decision(store, account, task_id, request_id, action):
            if action == "decide":
                raise sqlite3.OperationalError("synthetic interruption after acceptance")
            return advance(store, account, task_id, request_id, action)

        monkeypatch.setattr(cancellations, "_advance", interrupt_decision)
        assert client.post(path, headers=owner).status_code == 503
        before = history(database)
        assert client.post(path + "/seal", headers=owner).json()["state"] == "processing"
        assert history(database) == before
        if claim_first:
            publish(client, {GPU_1: "idle", GPU_2: "busy"})
            assert client.app.state.claim_head().task_id == task
        leases_before_decision = history(database)["gpu_leases"]
        assert len(leases_before_decision) == int(claim_first)

    monkeypatch.setattr(cancellations, "_advance", advance)
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        expected = "cancelled"
        before = history(database)
        if claim_first:
            old, = leases_before_decision
            new, = before["gpu_leases"]
            assert new[:5] == old[:5]
            assert new[5] == before["cancellation_requests"][0][5]
            assert new[6] == 1 and new[7:] == old[7:]
        else:
            assert before["gpu_leases"] == []
        (row,) = before["cancellation_requests"]
        assert row[:4] == ("demo-a", task, key, expected)
        assert row[4] < row[5]
        assert client.post(path, headers=owner).json()["state"] == expected
        detail = client.get(f"/api/tasks/{task}?request_id={key}", headers=owner).json()["task"]
        assert detail["status"] == "cancelled"
        assert not detail["can_cancel"]
        assert history(database) == before


@pytest.mark.parametrize("admitted", [False, True])
def test_nonowners_anonymous_and_invalid_csrf_cannot_change_requests(database, admitted):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        if admitted:
            publish(client, {GPU_1: "idle", GPU_2: "busy"})
            assert client.app.state.claim_head().task_id == task
        assert_view(client, owner, task, "admitted" if admitted else "queued", True)
        key = uuid4()
        path = command(task, key)
        assert client.post(path + "/seal", headers=owner).json()["state"] == "sealed"
        before = history(database)
        unknown_key = uuid4()
        for suffix in ("", "/seal"):
            for headers in ({}, {"X-CSRF-Token": "invalid"}):
                assert client.post(path + suffix, headers=headers).status_code == 403
            for name in ("demo-a-peer", "demo-b"):
                actor = login(client, name)
                for request_id in (key, unknown_key):
                    other = client.post(command(task, request_id) + suffix, headers=actor)
                    absent = client.post(command(uuid4(), request_id) + suffix, headers=actor)
                    assert other.status_code == absent.status_code == 404
                    assert other.json() == absent.json()
                    assert history(database) == before
            client.cookies.clear()
            assert client.post(path + suffix).status_code == 401
            login(client)
        assert history(database) == before


def test_pending_id_validation_matches_detail_and_commands(database):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        key = str(uuid4())
        receipt = client.post(command(task.upper(), key.upper()) + "/seal", headers=owner).json()
        assert receipt["task_id"] == task and receipt["request_id"] == key
        listing = client.get(
            "/api/tasks", params={"pending": f"{task}.{key}".upper()}, headers=owner
        ).json()
        detail = client.get(f"/api/tasks/{task.upper()}?request_id={key.upper()}", headers=owner)
        assert listing["tasks"] == [detail.json()["task"]]
        assert listing["tasks"][0]["cancellation"] == {"request_id": key, "state": "sealed"}
        missing = client.get("/api/tasks/not-a-uuid", headers=owner)
        assert missing.status_code == 404
        assert missing.json()["snapshot_version"] == listing["snapshot_version"]
        for invalid in (f"bad.{key}", f"{task}.bad", f"{task}.{key}.{key}"):
            assert (
                client.get("/api/tasks", params={"pending": invalid}, headers=owner).status_code
                == 422
            )


@pytest.mark.parametrize("seal", [False, True])
def test_terminal_replay_with_writer_holding_lock(database, seal):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        path = command(task, uuid4())
        receipt = client.post(path + ("/seal" if seal else ""), headers=owner).json()
        before = history(database)
        with closing(sqlite3.connect(database)) as holder:
            holder.execute("BEGIN IMMEDIATE")
            for suffix in ("", "/seal"):
                assert client.post(path + suffix, headers=owner).json() == receipt
        assert history(database) == before


@pytest.mark.parametrize("committed,read_busy", [(False, False), (True, False), (True, True)])
def test_commit_reply_loss_never_blindly_reissues_write(
    database, monkeypatch, committed, read_busy
):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        store = SubmissionStore(database)
        key = str(uuid4())
        writes = []

        class LostCommit(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "COMMIT":
                    writes.append(sql)
                    if committed:
                        super().execute(sql)
                    self.close()
                    raise sqlite3.OperationalError("synthetic lost commit reply")
                return super().execute(sql, parameters)

        original_reader = store._connect_readonly

        def reader():
            if read_busy and writes:
                error = sqlite3.OperationalError("synthetic reconciliation busy")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return original_reader()

        monkeypatch.setattr(store, "_connect_readonly", reader)
        monkeypatch.setattr(
            store,
            "_connect",
            lambda: sqlite3.connect(database, factory=LostCommit, autocommit=True),
        )

        def send():
            return cancellations.cancel(store, Account("demo-a", "group-a"), task, key, seal=True)

        if committed and not read_busy:
            assert send().state == "sealed"
        else:
            with pytest.raises(sqlite3.DatabaseError):
                send()
        assert writes == ["COMMIT"]
        assert len(history(database)["cancellation_requests"]) == int(committed)


def test_transient_failure_recovers_original_id_without_client_replay(database, monkeypatch):
    transition = cancellations._transition
    retry_started, healthy, finished = Event(), Event(), Event()
    attempts = []

    def fail_once(db, account, task_id, fact, action):
        if action == "decide":
            attempts.append(fact.request_id)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("synthetic decision outage")
            retry_started.set()
            assert healthy.wait(10)
        transition(db, account, task_id, fact, action)

    advance = cancellations._advance

    def observe_commit(store, account, task_id, request_id, action):
        receipt = advance(store, account, task_id, request_id, action)
        if action == "decide":
            finished.set()
        return receipt

    monkeypatch.setattr(cancellations, "_transition", fail_once)
    monkeypatch.setattr(cancellations, "_advance", observe_commit)
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        key = str(uuid4())
        assert client.post(command(task, key), headers=owner).status_code == 503
        try:
            assert retry_started.wait(10)
            before = history(database)
            fact = client.get(f"/api/tasks/{task}?request_id={key}", headers=owner).json()["task"]
            assert fact["cancellation"] == {"request_id": key, "state": "processing"}
            assert history(database) == before
        finally:
            healthy.set()
        assert finished.wait(10)
        (row,) = history(database)["cancellation_requests"]
        assert row[2:4] == (key, "cancelled")
        assert attempts == [key, key]


@pytest.mark.parametrize("first", ["accept", "seal"])
def test_overlapped_accept_seal_have_one_winner(database, monkeypatch, first):
    holding, competing, finish_decision = Event(), Event(), Event()
    transition = cancellations._transition
    advance = cancellations._advance_once
    second = "accept" if first == "seal" else "seal"

    def hold_write_lock(db, account, task_id, fact, action):
        if action == first:
            holding.set()
            assert competing.wait(10)
        transition(db, account, task_id, fact, action)

    def enter(store, account, task_id, request_id, action):
        if action == second:
            competing.set()
        if action == "decide":
            assert finish_decision.wait(10)
        return advance(store, account, task_id, request_id, action)

    monkeypatch.setattr(cancellations, "_transition", hold_write_lock)
    monkeypatch.setattr(cancellations, "_advance_once", enter)
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        key = str(uuid4())
        path = command(task, key)

        def send(action):
            return client.post(path + ("/seal" if action == "seal" else ""), headers=owner).json()

        before = history(database)
        with ThreadPoolExecutor(max_workers=2) as workers:
            one = workers.submit(send, first)
            assert holding.wait(10)
            two = workers.submit(send, second)
            try:
                sealed = (one if first == "seal" else two).result(10)
                expected = "sealed" if first == "seal" else "processing"
                assert sealed["state"] == expected
                pending = history(database)
                (row,) = pending["cancellation_requests"]
                assert row[2:4] == (key, expected)
                assert pending["tasks"] == before["tasks"]
            finally:
                finish_decision.set()
            result = (two if first == "seal" else one).result(10)
        after = history(database)
        (row,) = after["cancellation_requests"]
        assert result["state"] == row[3] == ("sealed" if first == "seal" else "cancelled")
        assert row[2] == key
        assert after["gpu_leases"] == after["simulated_start_events"] == []
        if first == "seal":
            assert after == pending
        else:
            assert after["queue_state"][0][-1] == before["queue_state"][0][-1] + 2


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("admitted", [False, True])
def test_http_cancel_reconciles_lost_decision_commit(database, monkeypatch, committed, admitted):
    original = SubmissionStore._connect
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        if admitted:
            publish(client, {GPU_1: "idle", GPU_2: "busy"})
            assert client.app.state.claim_head().task_id == task
        assert_view(client, owner, task, "admitted" if admitted else "queued", True)
        key = str(uuid4())
        before = history(database)
        decisions = []

        class LoseDecisionReply:
            def __init__(self, db):
                self.db = db
                self.deciding = False

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, sql, parameters=()):
                if "UPDATE cancellation_requests SET state=" in sql:
                    self.deciding = True
                if self.deciding and sql == "COMMIT":
                    decisions.append(key)
                    if committed:
                        self.db.execute(sql)
                    self.db.close()
                    raise sqlite3.OperationalError("synthetic lost decision commit reply")
                return self.db.execute(sql, parameters)

        monkeypatch.setattr(
            SubmissionStore, "_connect", lambda store: LoseDecisionReply(original(store))
        )
        monkeypatch.setattr(client.app.state, "wake_recovery", lambda: None)
        response = client.post(command(task, key), headers=owner)
        assert decisions == [key]
        after = history(database)
        (row,) = after["cancellation_requests"]
        assert row[2:4] == (key, "cancelled" if committed else "processing")
        assert after["simulated_start_events"] == []
        if admitted:
            old, = before["gpu_leases"]
            lease, = after["gpu_leases"]
            assert lease[:5] == old[:5]
            assert lease[5] == (row[5] if committed else None)
            assert lease[6] == int(committed)
            assert lease[7:] == old[7:]
        else:
            assert after["gpu_leases"] == []
        if committed:
            assert response.status_code == 200
            assert response.json()["state"] == "cancelled"
            assert int(response.json()["snapshot_version"]) == row[5]
            assert after["tasks"][0][7] == "cancelled"
        else:
            assert response.status_code == 503
            assert row[5] is None
            assert after["tasks"] == before["tasks"]
            assert after["queue_state"][0][-1] == before["queue_state"][0][-1] + 1


@pytest.mark.parametrize("first", ["cancel", "start"])
def test_admitted_cancel_start_commit_order_and_lost_receipt(database, monkeypatch, first):
    """A1–A3：受理不抢占裁决，独立连接只见原状态或完整终局。"""
    from labqueue.starts import StartBlocked, StartConfirmed

    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        a, b = [api_submit(client, owner)["task_id"] for _ in range(2)]
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        lease = client.app.state.claim_head()
        args = dict(task_id=a, claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid)
        account = Account("demo-a", "group-a")
        key = str(uuid4())
        assert_view(client, owner, a, "admitted", True)
        entered, proceed, start_entering = Event(), Event(), Event()
        start_commit, finish_start, cancel_entering = Event(), Event(), Event()
        caller = local()
        connect = SubmissionStore._connect

        class ObserveStartLock:
            def __init__(self, db):
                self.db = db

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE" and getattr(caller, "starting", False):
                    start_entering.set()
                if sql == "BEGIN IMMEDIATE" and getattr(caller, "deciding", False):
                    cancel_entering.set()
                if sql == "COMMIT" and first == "start" and getattr(caller, "starting", False):
                    start_commit.set()
                    assert finish_start.wait(10)
                return self.db.execute(sql, parameters)

        monkeypatch.setattr(
            SubmissionStore, "_connect", lambda store: ObserveStartLock(connect(store))
        )

        def competing_start():
            caller.starting = True
            return client.app.state.confirm_start(account, **args)
        original = cancellations._transition
        accepted = []

        def pause(db, actor, task_id, fact, action):
            if action == "decide":
                accepted.append(history(database))
                if first == "cancel":
                    original(db, actor, task_id, fact, action)
                    # 写入尚未提交，独立连接仍只见原任务、原租约和受理记录。
                    assert history(database) == accepted[-1]
                    entered.set()
                    assert proceed.wait(10)
                    return
            original(db, actor, task_id, fact, action)

        advance = cancellations._advance_once

        def pause_before_lock(store, actor, task_id, request_id, action):
            if first == "start" and action == "decide":
                entered.set()
                assert proceed.wait(10)
                caller.deciding = True
            return advance(store, actor, task_id, request_id, action)

        monkeypatch.setattr(cancellations, "_transition", pause)
        monkeypatch.setattr(cancellations, "_advance_once", pause_before_lock)
        with ThreadPoolExecutor(max_workers=2) as workers:
            cancellation = workers.submit(client.post, command(a, key), headers=owner)
            assert entered.wait(10)
            if first == "start":
                before = history(database)
                sealed = client.post(command(a, key) + "/seal", headers=owner).json()
                assert sealed["state"] == "processing"
                assert history(database) == before
                start_attempt = workers.submit(competing_start)
                assert start_commit.wait(10)
                assert history(database) == before
                proceed.set()
                assert cancel_entering.wait(10)
                assert history(database) == before
                finish_start.set()
                started = start_attempt.result(10)
                assert isinstance(started, StartConfirmed)
            else:
                start_attempt = workers.submit(competing_start)
                assert start_entering.wait(10)
            proceed.set()
            response = cancellation.result(10)
            assert response.status_code == 200
            # 丢弃命令响应，随后只读核对原 ID。
            committed = history(database)
            read = client.get(f"/api/tasks/{a}?request_id={key}", headers=owner).json()
            receipt = dict(read["task"]["cancellation"], task_id=a,
                           snapshot_version=read["snapshot_version"])
            assert history(database) == committed
            if first == "cancel":
                assert isinstance(start_attempt.result(10), StartBlocked)
                assert receipt["state"] == "cancelled"
                assert committed["simulated_start_events"] == []
                row, = committed["gpu_leases"]
                assert row[5] == committed["cancellation_requests"][0][5]
                assert row[6:9] == (1, 0, "not_started")
                assert int(receipt["snapshot_version"]) == row[5]
            else:
                assert receipt["state"] == "ineligible" and receipt["reason"] == "past_cutoff"
                assert len(committed["simulated_start_events"]) == 1
                assert committed["gpu_leases"][0][5] is None
                assert client.app.state.confirm_start(account, **args) == started
            for suffix in ("", "/seal"):
                assert client.post(command(a, key) + suffix, headers=owner).json() == receipt
            assert history(database) == committed
            detail = client.get(f"/api/tasks/{a}?request_id={key}", headers=owner).json()
            listing = client.get(
                "/api/tasks", params={"pending": f"{a}.{key}"}, headers=owner
            ).json()
            assert detail["task"] == listing["tasks"][0]
            assert not detail["task"]["can_cancel"]
            if first == "cancel":
                assert isinstance(client.app.state.confirm_start(account, **args), StartBlocked)
                next_lease = client.app.state.claim_head()
                assert (next_lease.task_id, next_lease.gpu_uuid) == (b, GPU_1)
                assert history(database)["gpu_leases"][0] == committed["gpu_leases"][0]


@pytest.mark.parametrize("fault", [
    "read_failure", "unknown", "pending", "confirmed_without_event", "running_without_event",
    "event_owner", "event_gpu", "event_claim", "event_without_confirmation",
    "release_failure", "task_failure", "resolution_failure", "version_failure",
])
def test_incomplete_start_evidence_or_release_failure_preserves_original_lease(
    database, monkeypatch, fault
):
    """A12 及回滚：证据缺失或矛盾不能推断原卡空闲。"""
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        lease = client.app.state.claim_head()
        if fault.startswith("event_"):
            client.app.state.confirm_start(
                Account("demo-a", "group-a"), task_id=task,
                claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid
            )
        with closing(sqlite3.connect(database)) as db:
            changes = {
                "unknown": "UPDATE gpu_leases SET execution_evidence='unknown'",
                "pending": "UPDATE gpu_leases SET reconciliation_pending=1",
                "confirmed_without_event": "UPDATE gpu_leases SET start_confirmed=1",
                "running_without_event": "UPDATE tasks SET status='running'",
                "event_owner": "UPDATE simulated_start_events SET execution_owner_id='demo-b'",
                "event_gpu": "UPDATE simulated_start_events SET gpu_uuid='wrong-gpu'",
                "event_claim": "UPDATE simulated_start_events SET claim_id='wrong-claim'",
                "event_without_confirmation": "UPDATE gpu_leases SET start_confirmed=0",
                "version_failure": """CREATE TRIGGER deny_version
                    BEFORE UPDATE OF snapshot_version ON queue_state
                    WHEN EXISTS (SELECT 1 FROM cancellation_requests WHERE state='cancelled')
                    BEGIN SELECT RAISE(IGNORE); END""",
                "task_failure": """CREATE TRIGGER deny_task BEFORE UPDATE OF status
                    ON tasks BEGIN SELECT RAISE(IGNORE); END""",
                "resolution_failure": """CREATE TRIGGER deny_resolution BEFORE UPDATE OF state
                    ON cancellation_requests BEGIN SELECT RAISE(IGNORE); END""",
                "release_failure": """CREATE TRIGGER deny_release BEFORE UPDATE OF released_version
                    ON gpu_leases BEGIN SELECT RAISE(IGNORE); END""",
            }
            if fault in changes:
                db.execute(changes[fault])
                db.commit()
        if fault == "read_failure":
            def unavailable(db, task_id):
                raise sqlite3.OperationalError("synthetic evidence read failure")
            monkeypatch.setattr(cancellations, "read_start", unavailable)
        monkeypatch.setattr(client.app.state, "wake_recovery", lambda: None)
        before = history(database)
        key = str(uuid4())
        assert client.post(command(task, key), headers=owner).status_code == 503
        after = history(database)
        for table in ("tasks", "gpu_leases", "simulated_start_events", "simulated_results"):
            assert after[table] == before[table]
        assert after["cancellation_requests"][0][2:4] == (key, "processing")
        assert after["cancellation_requests"][0][5] is None
        assert after["cancellation_recovery_blocks"] == [("demo-a", task, key)]
        assert after["queue_state"][0][-1] == before["queue_state"][0][-1] + 2


def test_old_database_migration_preserves_terminal_and_sealed_requests(database):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        terminal, sealed, cancelled, pending = [str(uuid4()) for _ in range(4)]
        client.post(command(task, terminal), headers=owner)
        client.post(command(task, sealed) + "/seal", headers=owner)
        cancelled_task = api_submit(client, owner)["task_id"]
        client.post(command(cancelled_task, cancelled), headers=owner)
        pending_task = api_submit(client, owner)["task_id"]
        cancellations._advance(SubmissionStore(database), Account("demo-a", "group-a"),
                               pending_task, pending, "accept")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "UPDATE cancellation_requests SET state='ineligible' WHERE request_id=?", (terminal,)
        )
        db.execute("ALTER TABLE cancellation_requests DROP COLUMN reason")
        db.commit()
    old = history(database)
    for _ in range(2):
        with TestClient(create_app(database), base_url=BASE_URL) as client:
            owner = login(client)
            before = history(database)
            migrated = [row[:-1] for row in before["cancellation_requests"] if row[2] != pending]
            assert migrated == [row for row in old["cancellation_requests"] if row[2] != pending]
            recovered, = [row for row in before["cancellation_requests"] if row[2] == pending]
            original, = [row for row in old["cancellation_requests"] if row[2] == pending]
            assert recovered[:3] == original[:3] and recovered[3] == "cancelled"
            assert recovered[4] == original[4] and recovered[5] > recovered[4]
            assert all(row[-1] is None for row in before["cancellation_requests"])
            for key, state in ((terminal, "ineligible"), (sealed, "sealed")):
                receipt = client.post(command(task, key), headers=owner).json()
                assert receipt["state"] == state and "reason" not in receipt
            for target, key in ((cancelled_task, cancelled), (pending_task, pending)):
                receipt = client.post(command(target, key), headers=owner).json()
                assert receipt["state"] == "cancelled"
            assert history(database) == before


def test_restart_recovers_accepted_id_after_start_won(database, monkeypatch):
    advance = cancellations._advance
    key = str(uuid4())
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        task = api_submit(client, headers)["task_id"]
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        lease = client.app.state.claim_head()

        def unavailable(store, account, task_id, request_id, action):
            if action == "decide":
                raise sqlite3.OperationalError("synthetic interruption")
            return advance(store, account, task_id, request_id, action)

        monkeypatch.setattr(cancellations, "_advance", unavailable)
        monkeypatch.setattr(client.app.state, "wake_recovery", lambda: None)
        assert client.post(command(task, key), headers=headers).status_code == 503
        client.app.state.confirm_start(Account("demo-a", "group-a"), task_id=task,
                                       claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid)
        before = history(database)
    monkeypatch.setattr(cancellations, "_advance", advance)
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        after = history(database)
        row, = after["cancellation_requests"]
        assert row[2:4] == (key, "ineligible") and row[-1] == "past_cutoff"
        assert after["gpu_leases"] == before["gpu_leases"]
        assert after["simulated_start_events"] == before["simulated_start_events"]
        for suffix in ("", "/seal"):
            receipt = client.post(command(task, key) + suffix, headers=headers).json()
            assert receipt["reason"] == "past_cutoff"
        assert history(database) == after


def test_admitted_seal_lost_receipt_manual_repeat_is_read_only(database):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        client.app.state.claim_head()
        key = str(uuid4())
        path = command(task, key) + "/seal"
        client.post(path, headers=owner)  # 模拟用户没有收到第一次封存回执。
        before = history(database)
        assert client.post(path, headers=owner).json()["state"] == "sealed"
        assert history(database) == before
        assert before["gpu_leases"][0][5] is None


def test_two_cancel_ids_race_release_original_lease_once(database, monkeypatch):
    from labqueue.starts import StartBlocked

    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        lease = client.app.state.claim_head()
        first, second = str(uuid4()), str(uuid4())
        holding, competing, proceed = Event(), Event(), Event()
        local_request = local()
        connect, advance = SubmissionStore._connect, cancellations._advance_once
        transition = cancellations._transition
        release_writes = []

        class ObserveWrites:
            def __init__(self, db):
                self.db = db

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE" and getattr(local_request, "id", None) == second:
                    competing.set()
                cursor = self.db.execute(sql, parameters)
                if "UPDATE gpu_leases SET startup_revoked=1" in sql:
                    release_writes.append(cursor.rowcount)
                return cursor

        def identify(store, actor, task_id, key, action):
            local_request.id = key
            return advance(store, actor, task_id, key, action)

        def pause(db, actor, task_id, fact, action):
            if fact.request_id == first and action == "decide":
                before = history(database)
                transition(db, actor, task_id, fact, action)
                assert history(database) == before
                holding.set()
                assert proceed.wait(10)
                return
            transition(db, actor, task_id, fact, action)

        monkeypatch.setattr(
            SubmissionStore, "_connect", lambda store: ObserveWrites(connect(store))
        )
        monkeypatch.setattr(cancellations, "_advance_once", identify)
        monkeypatch.setattr(cancellations, "_transition", pause)
        with ThreadPoolExecutor(max_workers=2) as workers:
            one = workers.submit(client.post, command(task, first), headers=owner)
            assert holding.wait(10)
            two = workers.submit(client.post, command(task, second), headers=owner)
            try:
                assert competing.wait(10)
            finally:
                proceed.set()
            assert one.result(10).json()["state"] == "cancelled"
            assert two.result(10).json()["state"] == "ineligible"
        before = history(database)
        assert release_writes == [1]
        row, = before["gpu_leases"]
        assert row[0] == lease.claim_id and row[5] is not None and row[6] == 1
        assert [(r[2], r[3]) for r in before["cancellation_requests"]] == [
            (first, "cancelled"), (second, "ineligible")
        ]
        assert all(r[5] is not None for r in before["cancellation_requests"])
        for key in (first, second):
            for suffix in ("", "/seal"):
                assert client.post(command(task, key) + suffix, headers=owner).status_code == 200
        assert isinstance(client.app.state.confirm_start(Account("demo-a", "group-a"),
            task_id=task, claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid), StartBlocked)
        assert history(database) == before
        assert release_writes == [1]
        assert_view(client, owner, task, "cancelled", False)


@pytest.mark.parametrize("restart", [False, True])
def test_recovery_isolates_failed_request_and_retries_without_unsafe_start(
    database, monkeypatch, restart
):
    from labqueue.leases import ClaimBlocked
    from labqueue.starts import StartBlocked

    healthy, b_recovered, a_recovered = Event(), Event(), Event()
    read_start, advance = cancellations.read_start, cancellations._advance
    with ExitStack() as stack:
        client = stack.enter_context(TestClient(create_app(database), base_url=BASE_URL))
        headers = login(client)
        a, b = [api_submit(client, headers)["task_id"] for _ in range(2)]
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        lease = client.app.state.claim_head()
        before_lease = history(database)["gpu_leases"]
        ka, kb = str(uuid4()), str(uuid4())
        store = client.app.state.confirm_start.__self__
        owner = Account("demo-a", "group-a")
        for task, key in ((a, ka), (b, kb)):
            cancellations._advance(store, owner, task, key, "accept")

        def faulty_read(db, task):
            if task == a and not healthy.is_set():
                raise sqlite3.OperationalError("synthetic per-task evidence outage")
            return read_start(db, task)

        def observe_recovery(store, account, task, key, action):
            result = advance(store, account, task, key, action)
            if action == "decide" and result.state == "cancelled":
                (a_recovered if task == a else b_recovered).set()
            return result

        monkeypatch.setattr(cancellations, "read_start", faulty_read)
        monkeypatch.setattr(cancellations, "_advance", observe_recovery)
        if restart:
            stack.close()
            client = stack.enter_context(TestClient(create_app(database), base_url=BASE_URL))
            headers = login(client)
        else:
            client.app.state.wake_recovery()
        assert b_recovered.wait(10)
        assert client.get("/health").status_code == 200
        assert_view(client, headers, a, "admitted", False)
        assert_view(client, headers, b, "cancelled", False)
        blocked = history(database)
        assert blocked["gpu_leases"] == before_lease
        assert blocked["cancellation_recovery_blocks"] == [("demo-a", a, ka)]
        assert blocked["simulated_start_events"] == []
        # 运行中分支仍在有效观测代际内，阻断来自原 ID 的持久标记。
        args = dict(task_id=a, claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid)
        start = client.app.state.confirm_start(owner, **args)
        assert start == StartBlocked("reconciliation_pending")
        api_submit(client, headers)
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        assert isinstance(client.app.state.claim_head(), ClaimBlocked)
        assert history(database)["gpu_leases"] == before_lease
        b_terminal = next(row for row in blocked["cancellation_requests"] if row[2] == kb)
        stack.close()
        client = stack.enter_context(TestClient(create_app(database), base_url=BASE_URL))
        headers = login(client)
        retained = history(database)
        assert retained["cancellation_recovery_blocks"] == [("demo-a", a, ka)]
        assert retained["gpu_leases"] == before_lease
        assert next(row for row in retained["cancellation_requests"] if row[2] == kb) == b_terminal
        assert_view(client, headers, a, "admitted", False)
        assert client.app.state.confirm_start(owner, **args) == StartBlocked(
            "reconciliation_pending"
        )
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        assert isinstance(client.app.state.claim_head(), ClaimBlocked)
        assert history(database)["gpu_leases"] == before_lease
        healthy.set()
        assert a_recovered.wait(10)  # 无客户端重发，由服务定时重试。
        after = history(database)
        assert after["cancellation_recovery_blocks"] == []
        request = next(row for row in after["cancellation_requests"] if row[2] == ka)
        assert request[3] == "cancelled" and request[5] == after["gpu_leases"][0][5]
        for task, key in ((a, ka), (b, kb)):
            assert client.post(command(task, key), headers=headers).json()["state"] == "cancelled"
        assert history(database) == after


@pytest.mark.parametrize("rejection", ["ABORT", "IGNORE"])
def test_uncommittable_recovery_block_fails_closed(database, monkeypatch, rejection):
    from labqueue.leases import ClaimBlocked
    from labqueue.starts import StartBlocked

    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        task = api_submit(client, headers)["task_id"]
        publish(client, {GPU_1: "idle", GPU_2: "idle"})
        lease = client.app.state.claim_head()
        store = client.app.state.confirm_start.__self__
        owner = Account("demo-a", "group-a")
        key = str(uuid4())
        cancellations._advance(store, owner, task, key, "accept")
        with closing(sqlite3.connect(database)) as db:
            failure = "ABORT, 'synthetic block failure'" if rejection == "ABORT" else "IGNORE"
            db.execute(f"""CREATE TRIGGER deny_block BEFORE INSERT ON cancellation_recovery_blocks
                          BEGIN SELECT RAISE({failure}); END""")
        before = history(database)
        with monkeypatch.context() as patch:
            def fail(db, task_id):
                raise sqlite3.OperationalError("synthetic evidence failure")
            patch.setattr(cancellations, "read_start", fail)
            message = (
                "synthetic block failure" if rejection == "ABORT" else "无法证实取消恢复阻断记录"
            )
            with pytest.raises(sqlite3.Error, match=message):
                cancellations.recover_cancellations(store)
        assert history(database) == before
        assert client.app.state.confirm_start(owner, task_id=task, claim_id=lease.claim_id,
            gpu_uuid=lease.gpu_uuid) == StartBlocked("execution_unverified")
        assert client.app.state.claim_head() == ClaimBlocked("gpu_visibility_unknown")
        assert history(database) == before


def test_queued_recovery_block_repeated_failure_and_success(database, monkeypatch):
    from labqueue.leases import ClaimBlocked

    finished = Event()
    advance = cancellations._advance

    def observe(store, owner, task, key, action):
        result = advance(store, owner, task, key, action)
        if action == "decide" and result.state == "cancelled":
            finished.set()
        return result

    monkeypatch.setattr(cancellations, "_advance", observe)
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        task = api_submit(client, headers)["task_id"]
        store = client.app.state.confirm_start.__self__
        key = str(uuid4())
        cancellations._advance(store, Account("demo-a", "group-a"), task, key, "accept")
        with monkeypatch.context() as patch:
            def fail(db, task_id):
                raise sqlite3.OperationalError("synthetic queued evidence outage")
            patch.setattr(cancellations, "read_start", fail)
            assert cancellations.recover_cancellations(store)
            before = history(database)
            assert cancellations.recover_cancellations(store)
            assert history(database) == before
            assert_view(client, headers, task, "queued", False)
            publish(client, {GPU_1: "idle", GPU_2: "idle"})
            before_claim = history(database)
            assert client.app.state.claim_head() == ClaimBlocked("gpu_visibility_unknown")
            assert history(database) == before_claim
            assert before_claim["gpu_leases"] == []
        client.app.state.wake_recovery()
        assert finished.wait(10)
        after = history(database)
        assert after["cancellation_recovery_blocks"] == []
        assert after["cancellation_requests"][0][2:4] == (key, "cancelled")
        assert_view(client, headers, task, "cancelled", False)
        assert not cancellations.recover_cancellations(store)
        assert history(database) == after


def test_blocked_recovery_allows_only_confirmed_start_replay(database, monkeypatch):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        task = api_submit(client, headers)["task_id"]
        publish(client, {GPU_1: "idle", GPU_2: "busy"})
        lease = client.app.state.claim_head()
        owner = Account("demo-a", "group-a")
        args = dict(task_id=task, claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid)
        started = client.app.state.confirm_start(owner, **args)
        store = client.app.state.confirm_start.__self__
        key = str(uuid4())
        cancellations._advance(store, owner, task, key, "accept")
        with monkeypatch.context() as patch:
            def fail(db, task_id):
                raise sqlite3.OperationalError("synthetic confirmed evidence read failure")
            patch.setattr(cancellations, "read_start", fail)
            assert cancellations.recover_cancellations(store)
            before = history(database)
            assert client.app.state.confirm_start(owner, **args) == started
            assert history(database) == before
        assert not cancellations.recover_cancellations(store)
        after = history(database)
        assert after["cancellation_recovery_blocks"] == []
        assert after["gpu_leases"] == before["gpu_leases"]
        assert after["simulated_start_events"] == before["simulated_start_events"]
        assert after["cancellation_requests"][0][3] == "ineligible"
        assert after["cancellation_requests"][0][6] == "past_cutoff"


@pytest.mark.parametrize("committed", [False, True])
def test_recovery_block_commit_failure_stays_closed_until_restart(database, monkeypatch, committed):
    from labqueue.leases import ClaimBlocked
    from labqueue.starts import StartBlocked

    healthy, recovered = Event(), Event()
    read_start, advance = cancellations.read_start, cancellations._advance
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        task = api_submit(client, headers)["task_id"]
        publish(client, {GPU_1: "idle", GPU_2: "idle"})
        lease = client.app.state.claim_head()
        store = client.app.state.confirm_start.__self__
        owner = Account("demo-a", "group-a")
        key = str(uuid4())
        cancellations._advance(store, owner, task, key, "accept")
        before = history(database)
        connect = store._connect
        commits = []

        def faulty_read(db, task_id):
            if task_id == task and not healthy.is_set():
                raise sqlite3.OperationalError("synthetic evidence outage")
            return read_start(db, task_id)

        class LoseBlockCommit:
            def __init__(self, db):
                self.db = db
                self.blocking = False

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, sql, parameters=()):
                if "INSERT OR IGNORE INTO cancellation_recovery_blocks" in sql:
                    self.blocking = True
                if self.blocking and sql == "COMMIT":
                    assert history(database) == before
                    commits.append(committed)
                    if committed:
                        self.db.execute(sql)
                    self.db.close()
                    raise sqlite3.OperationalError("synthetic lost block commit")
                return self.db.execute(sql, parameters)

        monkeypatch.setattr(cancellations, "read_start", faulty_read)
        with monkeypatch.context() as patch:
            patch.setattr(store, "_connect", lambda: LoseBlockCommit(connect()))
            with pytest.raises(sqlite3.Error, match="synthetic lost block commit"):
                cancellations.recover_cancellations(store)
        assert commits == [committed]
        assert store.cancellation_storage_failed
        after = history(database)
        expected_blocks = [("demo-a", task, key)] if committed else []
        assert after["cancellation_recovery_blocks"] == expected_blocks
        for table in ("tasks", "gpu_leases", "simulated_start_events", "cancellation_requests"):
            assert after[table] == before[table]
        assert after["queue_state"][0][-1] == before["queue_state"][0][-1] + int(committed)
        args = dict(task_id=task, claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid)
        assert client.app.state.confirm_start(owner, **args) == StartBlocked("execution_unverified")
        assert client.app.state.claim_head() == ClaimBlocked("gpu_visibility_unknown")
        assert history(database) == after

    def observe(store, account, task_id, request_id, action):
        result = advance(store, account, task_id, request_id, action)
        if action == "decide" and result.state == "cancelled":
            recovered.set()
        return result

    monkeypatch.setattr(cancellations, "_advance", observe)
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        isolated = history(database)
        assert isolated["cancellation_recovery_blocks"] == [("demo-a", task, key)]
        assert isolated["gpu_leases"] == before["gpu_leases"]
        assert_view(client, headers, task, "admitted", False)
        assert client.app.state.confirm_start(owner, **args) == StartBlocked(
            "reconciliation_pending"
        )
        healthy.set()
        assert recovered.wait(10)
        resolved = history(database)
        assert resolved["cancellation_recovery_blocks"] == []
        assert resolved["cancellation_requests"][0][2:4] == (key, "cancelled")
        assert resolved["gpu_leases"][0][5] == resolved["cancellation_requests"][0][5]
        assert client.post(command(task, key), headers=headers).json()["state"] == "cancelled"
        assert history(database) == resolved


@pytest.mark.parametrize("fault", ["close_on_commit", "rollback_on_insert"])
def test_execution_entries_during_unreported_block_failure(database, monkeypatch, fault):
    """数据库已释放写权，阻断写异常仍停在驱动层时，两个执行入口同时到达。"""
    from labqueue.leases import ClaimBlocked
    from labqueue.starts import StartBlocked

    owner = Account("demo-a", "group-a")
    key = str(uuid4())
    database_unlocked, propagate, acquired_writer = Event(), Event(), Event()
    at_begin = {name: Event() for name in ("start", "claim")}
    caller = local()
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        a, b = [api_submit(client, headers)["task_id"] for _ in range(2)]
        publish(client, {GPU_1: "idle", GPU_2: "idle"})
        lease = client.app.state.claim_head()
        args = dict(task_id=a, claim_id=lease.claim_id, gpu_uuid=lease.gpu_uuid)
        store = client.app.state.confirm_start.__self__
        cancellations._advance(store, owner, a, key, "accept")
        before = history(database)
        connect = store._connect

        if fault == "rollback_on_insert":
            with closing(sqlite3.connect(database)) as db:
                db.execute("""CREATE TRIGGER rollback_block
                              BEFORE INSERT ON cancellation_recovery_blocks
                              BEGIN SELECT RAISE(ROLLBACK, 'injected rollback'); END""")

        class PauseFault:
            def __init__(self, db):
                self.db = db
                self.inserting_block = False

            def __getattr__(self, name):
                return getattr(self.db, name)

            def execute(self, sql, parameters=()):
                action = getattr(caller, "action", None)
                if sql == "BEGIN IMMEDIATE" and action in at_begin:
                    at_begin[action].set()
                    cursor = self.db.execute(sql, parameters)
                    acquired_writer.set()
                    return cursor
                if "INSERT OR IGNORE INTO cancellation_recovery_blocks" in sql:
                    self.inserting_block = True
                    if fault == "rollback_on_insert":
                        try:
                            return self.db.execute(sql, parameters)
                        except sqlite3.IntegrityError:
                            assert not self.db.in_transaction
                            database_unlocked.set()
                            assert propagate.wait(10)
                            raise
                if self.inserting_block and fault == "close_on_commit" and sql == "COMMIT":
                    self.db.close()
                    database_unlocked.set()
                    assert propagate.wait(10)
                    raise sqlite3.OperationalError("injected close")
                return self.db.execute(sql, parameters)

        def unavailable(db, task_id):
            raise sqlite3.OperationalError("injected evidence failure")

        def compete(action):
            caller.action = action
            if action == "start":
                return client.app.state.confirm_start(owner, **args)
            return client.app.state.claim_head()

        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=3) as workers:
            patch.setattr(store, "_connect", lambda: PauseFault(connect()))
            patch.setattr(cancellations, "read_start", unavailable)
            recovery = workers.submit(cancellations.recover_cancellations, store)
            try:
                assert database_unlocked.wait(10)
                contenders = {name: workers.submit(compete, name) for name in at_begin}
                assert all(event.wait(10) for event in at_begin.values())
                assert acquired_writer.wait(10)
                assert not store.cancellation_storage_failed
                assert history(database) == before
            finally:
                propagate.set()
            with pytest.raises(sqlite3.Error, match="injected"):
                recovery.result(10)
            assert contenders["start"].result(10) == StartBlocked("execution_unverified")
            assert contenders["claim"].result(10) == ClaimBlocked("gpu_visibility_unknown")
        assert store.cancellation_storage_failed
        assert compete("start") == StartBlocked("execution_unverified")
        assert compete("claim") == ClaimBlocked("gpu_visibility_unknown")
        assert history(database) == before

    # 故障解除后用原库重新启动；初始化先恢复原 ID，再开放模拟派发。
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        headers = login(client)
        fact = client.get(f"/api/tasks/{a}?request_id={key}", headers=headers).json()["task"]
        assert fact["cancellation"] == {"request_id": key, "state": "cancelled"}
        recovered = history(database)
        assert recovered["simulated_start_events"] == []
        assert recovered["gpu_leases"][0][5] == recovered["cancellation_requests"][0][5]
        publish(client, {GPU_1: "idle", GPU_2: "idle"})
        assert client.app.state.claim_head().task_id == b
