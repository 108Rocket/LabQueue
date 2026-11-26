"""Story 3.1 的 HTTP 命令、已提交历史及重启核对。"""

import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from threading import Event
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
    )
    with closing(sqlite3.connect(path)) as observer:
        observer.execute("BEGIN")
        return {t: observer.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in tables}


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


def test_unknown_refresh_then_seal_survives_restart_and_late_request(database):
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
            path = command(task, key)
            delayed = workers.submit(deliver_late, path)
            assert held.wait(10)
            before = history(database)
            for _ in range(2):
                detail = client.get(f"/api/tasks/{task}?request_id={key}", headers=owner).json()
                assert detail["task"]["cancellation"] == {"request_id": key, "state": "unknown"}
            assert history(database) == before
            assert client.post(path + "/seal", headers=owner).json()["state"] == "sealed"

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
        transition = cancellations._transition

        def interrupt_decision(db, account, task_id, fact, action):
            if action == "decide":
                raise sqlite3.OperationalError("synthetic interruption after acceptance")
            transition(db, account, task_id, fact, action)

        monkeypatch.setattr(cancellations, "_transition", interrupt_decision)
        assert client.post(path, headers=owner).status_code == 503
        before = history(database)
        assert client.post(path + "/seal", headers=owner).json()["state"] == "processing"
        assert history(database) == before
        if claim_first:
            publish(client, {GPU_1: "idle", GPU_2: "busy"})
            assert client.app.state.claim_head().task_id == task
        leases_before_decision = history(database)["gpu_leases"]
        assert len(leases_before_decision) == int(claim_first)

    monkeypatch.setattr(cancellations, "_transition", transition)
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        expected = "ineligible" if claim_first else "cancelled"
        before = history(database)
        assert before["gpu_leases"] == leases_before_decision
        (row,) = before["cancellation_requests"]
        assert row[:4] == ("demo-a", task, key, expected)
        assert row[4] < row[5]
        assert client.post(path, headers=owner).json()["state"] == expected
        detail = client.get(f"/api/tasks/{task}?request_id={key}", headers=owner).json()["task"]
        assert detail["status"] == ("admitted" if claim_first else "cancelled")
        assert not detail["can_cancel"]
        assert history(database) == before


def test_nonowners_anonymous_and_invalid_csrf_cannot_change_requests(database):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
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
def test_http_cancel_reconciles_lost_decision_commit(database, monkeypatch, committed):
    original = SubmissionStore._connect
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        owner = login(client)
        task = api_submit(client, owner)["task_id"]
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
        assert after["gpu_leases"] == after["simulated_start_events"] == []
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
