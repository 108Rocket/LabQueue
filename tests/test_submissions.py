import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient

import labqueue.submissions as submissions
from labqueue.app import create_app, require_csrf
from labqueue.auth import VIEW_SCOPE_COOKIE, Session
from labqueue.observations import (
    OBSERVATION_TTL_SECONDS,
    SIMULATED_GPU_UUIDS,
    PublicationCommitUnknown,
    StalePublisher,
    read_admission,
)
from labqueue.storage import default_database, verify_storage
from labqueue.submissions import SubmissionStore

BASE_URL = "http://127.0.0.1"
GPU_1, GPU_2 = SIMULATED_GPU_UUIDS
ACCOUNT_PASSWORDS = {
    "demo-a": "synthetic-demo-a",
    "demo-a-peer": "synthetic-demo-a-peer",
    "demo-b": "synthetic-demo-b",
}


@pytest.fixture
def database(tmp_path, monkeypatch):
    for account, password in ACCOUNT_PASSWORDS.items():
        monkeypatch.setenv(f"LABQUEUE_{account.upper().replace('-', '_')}_PASSWORD", password)
    return tmp_path / "private" / "test.sqlite3"


@pytest.fixture
def client(database):
    with TestClient(create_app(database), base_url=BASE_URL) as client:
        yield client


def login(client, account="demo-a"):
    response = client.post(
        "/api/session",
        headers={"X-LabQueue-Request": "1"},
        json={"username": account, "password": ACCOUNT_PASSWORDS[account]},
    )
    assert response.status_code == 200
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def payload(**changes):
    return {
        "submission_key": str(uuid4()),
        "name": "合成任务",
        "command": "echo synthetic-private-marker",
        **changes,
    }


def state(path):
    with closing(sqlite3.connect(path)) as db:
        tasks = db.execute("""
            SELECT owner_id, group_id, name, command, memory_gib, status, enqueue_seq
            FROM tasks ORDER BY enqueue_seq
            """).fetchall()
        counters = db.execute("SELECT enqueue_seq, snapshot_version FROM queue_state").fetchone()
        return tasks, counters


def test_private_store_includes_live_wal_and_shm(database):
    store = SubmissionStore(database)
    store.initialize()
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        for suffix in ("", "-wal", "-shm"):
            assert database.with_name(database.name + suffix).is_file()
        verify_storage(database)
        db.execute("ROLLBACK")


def test_default_database_is_under_account_profile(tmp_path, monkeypatch):
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert default_database() == tmp_path / "LabQueue" / "labqueue.sqlite3"
    else:
        from pathlib import Path

        assert default_database().is_relative_to(Path.home())


@pytest.mark.parametrize("target", ["directory", "main", "-wal", "-shm"])
def test_permissive_database_is_rejected_before_use(database, target):
    store = SubmissionStore(database)
    store.initialize()
    with closing(store._connect()) as db:
        db.execute("BEGIN IMMEDIATE")
        paths = {"directory": database.parent, "main": database}
        path = paths.get(target, database.with_name(database.name + target))
        assert path.exists()
        if os.name == "nt":
            # 仅修改测试文件，验证真实 ACL 过宽时启动失败。
            subprocess.run(["icacls", str(path), "/grant", "*S-1-5-32-545:R"], check=True)
        else:
            path.chmod(0o755 if path.is_dir() else 0o644)
        with pytest.raises(PermissionError):
            store.initialize()


def test_distinct_decimal_json_numbers_conflict_without_float_rounding(client, database):
    headers = {**login(client), "Content-Type": "application/json"}
    body = json.dumps(payload())[:-1] + ', "memory_gib": '
    original = "1.00000000000000000001"
    changed = "1.00000000000000000002"

    def submit(number):
        return client.post("/api/submissions", content=body + number + "}", headers=headers)

    receipt = submit(original)
    assert receipt.status_code == 200 and receipt.json()["memory_gib"] == original
    assert submit(changed).status_code == 409
    assert submit(original + "0").json() == receipt.json()
    assert state(database)[1] == (1, 1)


def test_old_page_session_cannot_read_new_accounts_receipt(client):
    old = login(client)
    body = payload()
    first = client.post("/api/submissions", json=body, headers=old).json()
    current = login(client, "demo-b")
    second = client.post("/api/submissions", json=body, headers=current).json()
    assert first["task_id"] != second["task_id"]
    path = f"/api/submissions/{body['submission_key']}"
    denied = client.get(path, headers=old)
    assert denied.status_code == 403 and second["task_id"] not in denied.text
    assert client.get(path, headers=current).json() == second


def test_unknown_memory_is_persisted_and_owner_can_verify(client, database):
    publish(client, {GPU_1: "busy", GPU_2: "busy"})
    headers = login(client)
    body = payload(memory_gib=None)
    response = client.post("/api/submissions", json=body, headers=headers)
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["status"] == "queued"
    assert receipt["memory_gib"] is None
    assert receipt["enqueue_seq"] == 1
    assert response.headers["cache-control"] == "no-store"
    assert body["command"] not in response.text
    assert client.get(f"/api/submissions/{body['submission_key']}").json() == receipt
    assert state(database) == (
        [
            ("demo-a", "group-a", body["name"], body["command"], None, "queued", 1),
        ],
        (1, 2),
    )
    with closing(SubmissionStore(database)._connect()) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
        queued_at = db.execute("SELECT queued_at FROM tasks").fetchone()[0]
        assert datetime.fromisoformat(queued_at).utcoffset() == timedelta(0)


def test_anonymous_invalid_session_csrf_and_forged_fields_cannot_enqueue(client, database):
    body = payload()
    forged = {**body, "owner_id": "demo-b", "group_id": "group-b"}
    assert client.post("/api/submissions", json=forged).status_code == 401
    assert client.get(f"/api/submissions/{body['submission_key']}").status_code == 401
    credentials = {"username": "demo-a", "password": ACCOUNT_PASSWORDS["demo-a"]}
    assert client.post("/api/session", json=credentials).status_code == 403
    credentials["password"] = "incorrect"
    response = client.post("/api/session", json=credentials, headers={"X-LabQueue-Request": "1"})
    assert response.status_code == 401
    headers = login(client)
    assert client.post("/api/submissions", json=body).status_code == 403
    assert (
        client.post("/api/submissions", json=body, headers={"X-CSRF-Token": "bad"}).status_code
        == 403
    )
    response = client.post("/api/submissions", json=forged, headers=headers)
    assert response.status_code == 422
    assert "demo-b" not in response.text and body["command"] not in response.text
    assert state(database) == ([], (0, 0))


def test_lost_receipt_retries_after_restart_and_conflicting_content_cannot_overwrite(database):
    body = payload()
    with TestClient(create_app(database), base_url=BASE_URL) as old_client:
        headers = login(old_client)
        assert old_client.post("/api/submissions", json=body, headers=headers).status_code == 200
        old_session = old_client.cookies.get("labqueue_session")
    assert len(state(database)[0]) == 1

    with TestClient(create_app(database), base_url=BASE_URL) as client:
        client.cookies.set("labqueue_session", old_session)
        path = f"/api/submissions/{body['submission_key']}"
        assert client.get(path).status_code == 401
        client.cookies.clear()
        headers = login(client)
        original = client.get(path).json()
        assert original["enqueue_seq"] == 1
        response = client.post("/api/submissions", json=body, headers=headers)
        assert response.status_code == 200 and response.json() == original
        before = state(database)
        for change in ({"name": "另一任务"}, {"command": "echo changed"}, {"memory_gib": 1}):
            response = client.post("/api/submissions", json={**body, **change}, headers=headers)
            assert response.status_code == 409
        assert state(database) == before
        assert client.get(path).json() == original
        sequential = client.post("/api/submissions", json=payload(), headers=headers)
        assert sequential.json()["enqueue_seq"] == 2
    assert state(database)[1] == (2, 2)


def test_concurrent_retries_enqueue_once(client, database):
    headers = login(client)
    body = payload()
    ready = Barrier(6, timeout=10)

    def submit(_index):
        ready.wait()
        response = client.post("/api/submissions", json=body, headers=headers)
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, range(6)))
    assert all(receipt == results[0] for receipt in results)
    assert len(state(database)[0]) == 1
    assert state(database)[1] == (1, 1)


def test_key_and_readback_are_owner_scoped(client, database):
    headers = login(client)
    body = payload()
    first = client.post("/api/submissions", json=body, headers=headers).json()
    path = f"/api/submissions/{body['submission_key']}"
    assert client.delete("/api/session").status_code == 403
    assert client.delete("/api/session", headers=headers).status_code == 204
    assert client.get(path).status_code == 401

    headers = login(client, "demo-b")
    denied = client.get(path)
    missing = client.get(f"/api/submissions/{uuid4()}")
    assert denied.status_code == missing.status_code == 404
    assert denied.json() == missing.json()
    second = client.post("/api/submissions", json=body, headers=headers).json()
    assert second["task_id"] != first["task_id"]
    assert second["enqueue_seq"] == 2
    assert client.get(path).json() == second
    assert [task[:2] for task in state(database)[0]] == [
        ("demo-a", "group-a"),
        ("demo-b", "group-b"),
    ]


@pytest.mark.parametrize("value", ["", 0, -1, "NaN", "Infinity", "1 GB"])
def test_invalid_estimates_return_field_feedback_without_mutation(client, database, value):
    response = client.post(
        "/api/submissions", json=payload(memory_gib=value), headers=login(client)
    )
    assert response.status_code == 422
    assert set(response.json()["fields"]) == {"memory_gib"}
    assert "synthetic-private-marker" not in response.text
    assert state(database) == ([], (0, 0))


def test_positive_estimate_is_stored_and_equivalent_decimal_replays(client, database):
    headers = login(client)
    body = payload(memory_gib="1.875")
    response = client.post("/api/submissions", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["memory_gib"] == "1.875"
    replay = client.post("/api/submissions", json={**body, "memory_gib": "1.8750"}, headers=headers)
    assert replay.status_code == 200 and replay.json() == response.json()
    assert state(database)[0][0][4] == "1.875"


def test_insert_failure_does_not_advance_sequence_or_snapshot(client, database):
    headers = login(client)
    body = payload()
    with closing(sqlite3.connect(database)) as db:
        db.executescript("""
            CREATE TRIGGER reject_insert BEFORE INSERT ON tasks
            BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END;
            """)
    assert client.post("/api/submissions", json=body, headers=headers).status_code == 503
    assert state(database) == ([], (0, 0))
    with closing(sqlite3.connect(database)) as db:
        db.execute("DROP TRIGGER reject_insert")
    response = client.post("/api/submissions", json=body, headers=headers)
    assert response.status_code == 200 and response.json()["enqueue_seq"] == 1


def test_uncertain_commit_is_checked_in_a_new_connection(client, database, monkeypatch):
    headers = login(client)
    original_connect = sqlite3.connect

    class LoseCommitResult(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if sql == "COMMIT":
                raise sqlite3.OperationalError("synthetic lost commit result")
            return result

    connections = []

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs, factory=LoseCommitResult)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    response = client.post("/api/submissions", json=payload(), headers=headers)
    assert response.status_code == 200 and response.json()["enqueue_seq"] == 1
    assert len(connections) == 2
    assert state(database)[1] == (1, 1)


def test_busy_transaction_retries_from_start(client, database, monkeypatch):
    headers = login(client)
    body = payload()
    original_connect = sqlite3.connect
    starts = []

    with closing(original_connect(database, autocommit=True, check_same_thread=False)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")

        class ContendedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "BEGIN IMMEDIATE":
                    starts.append(self)
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError as error:
                    assert sql == "BEGIN IMMEDIATE"
                    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                    blocker.execute("ROLLBACK")
                    raise

        def connect(*args, **kwargs):
            kwargs["timeout"] = 0
            return original_connect(*args, **kwargs, factory=ContendedConnection)

        monkeypatch.setattr(sqlite3, "connect", connect)
        response = client.post("/api/submissions", json=body, headers=headers)

    assert response.status_code == 200 and response.json()["enqueue_seq"] == 1
    assert len(starts) == 2 and starts[0] is not starts[1]
    assert state(database)[1] == (1, 1)


def test_submission_page_requires_login_and_uses_the_authenticated_session(client):
    anonymous = client.get("/")
    assert 'id="login"' in anonymous.text and 'id="submission"' not in anonymous.text
    headers = login(client)
    page = client.get("/")
    assert 'data-owner="demo-a"' in page.text and 'id="submission"' in page.text
    assert 'id="lookup"' in page.text and headers["X-CSRF-Token"] in page.text
    assert client.get("/static/submission.js").status_code == 200


def api_submit(client, headers, **changes):
    response = client.post("/api/submissions", json=payload(**changes), headers=headers)
    assert response.status_code == 200
    return response.json()


def publish(client, reports):
    publisher = client.app.state.observations
    token = publisher.begin_collection()
    assert publisher.publish(token, reports)
    return token


def committed_admission(database):
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        admission = read_admission(db)
        version = db.execute("SELECT snapshot_version FROM queue_state WHERE id = 1").fetchone()[0]
    return admission, version


def assert_owner_admission(client, database, headers, receipt, reason, candidates=()):
    admission, version = committed_admission(database)
    assert (admission.waiting_reason, admission.candidate_uuids) == (reason, candidates)
    listing = client.get("/api/tasks", headers=headers).json()
    assert listing["snapshot_version"] == str(version)
    task, = listing["tasks"]
    assert (task["task_id"], task["enqueue_seq"], task["queue_position"]) == (
        receipt["task_id"], str(receipt["enqueue_seq"]), 2,
    )
    assert task["waiting_reason"] == reason
    detail = client.get(f"/api/tasks/{receipt['task_id']}", headers=headers).json()
    assert detail == {"snapshot_version": str(version), "task": task}
    assert committed_admission(database) == (admission, version)
    return admission, version


@pytest.fixture
def queued_task(client):
    api_submit(client, login(client, "demo-b"))
    headers = login(client)
    return headers, api_submit(client, headers)


@pytest.mark.parametrize(
    ("reports", "reason"),
    [
        (None, "gpu_monitoring_failed"),
        ({GPU_1: "idle"}, "gpu_monitoring_failed"),
        ({GPU_2: "idle"}, "gpu_monitoring_failed"),
        ({GPU_1: "unknown", GPU_2: "idle"}, "gpu_visibility_unknown"),
        ({GPU_1: "idle", GPU_2: "unknown"}, "gpu_visibility_unknown"),
    ],
)
def test_failed_incomplete_or_unknown_observation_closes_both_gpus(
    client, database, queued_task, reports, reason
):
    headers, receipt = queued_task
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    available, old_version = assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_1,)
    )

    token = publish(client, reports)
    blocked, version = assert_owner_admission(client, database, headers, receipt, reason)
    assert blocked.admission_generation == available.admission_generation + 1
    assert blocked.published_sequence == token.sequence
    assert version == old_version + 1


@pytest.mark.parametrize(
    ("reports", "reason", "candidates"),
    [
        ({GPU_1: "idle", GPU_2: "busy"}, "waiting_dispatch", (GPU_1,)),
        ({GPU_1: "busy", GPU_2: "busy"}, "gpu_busy", ()),
    ],
)
def test_observation_expires_once_at_its_collection_deadline(
    client, database, queued_task, reports, reason, candidates
):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    publisher.advance_clock(100)
    token = publisher.begin_collection()
    assert (token.started_at, token.deadline) == (100, 100 + OBSERVATION_TTL_SECONDS)
    publisher.advance_clock(101)
    assert publisher.publish(token, reports)
    available, version = assert_owner_admission(
        client, database, headers, receipt, reason, candidates
    )
    assert available.clock == 101
    assert available.deadline == token.deadline

    publisher.advance_clock(token.deadline - 1)
    before_deadline = assert_owner_admission(
        client, database, headers, receipt, reason, candidates
    )
    assert before_deadline[0].admission_generation == available.admission_generation
    assert before_deadline[1] == version

    publisher.advance_clock(token.deadline)
    expired = assert_owner_admission(
        client, database, headers, receipt, "gpu_observation_expired"
    )
    assert expired[0].clock == token.deadline
    assert expired[0].admission_generation == available.admission_generation + 1
    assert expired[1] == version + 1
    for time in (token.deadline - 1, token.deadline, token.deadline + 1):
        publisher.advance_clock(time)
        current = assert_owner_admission(
            client, database, headers, receipt, "gpu_observation_expired"
        )
        assert current[0].clock == max(time, token.deadline)
        assert current[0].admission_generation == expired[0].admission_generation
        assert current[1] == expired[1]


def test_expired_collection_cannot_restore_admission(client, database, queued_task):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    delayed = publisher.begin_collection()
    publisher.advance_clock(delayed.deadline)
    assert publisher.publish(delayed, {GPU_1: "idle", GPU_2: "idle"})
    assert_owner_admission(client, database, headers, receipt, "gpu_observation_expired")


def test_observation_rejects_foreign_tokens_and_modified_collection_times(
    client, database, queued_task
):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    local = publisher.begin_collection()
    publisher.advance_clock(local.deadline)
    assert publisher.publish(local, {GPU_1: "idle", GPU_2: "busy"})
    local_pending = publisher.begin_collection()
    expired = assert_owner_admission(
        client, database, headers, receipt, "gpu_observation_expired"
    )

    foreign_path = database.parent.parent / "other" / "same-sequence.sqlite3"
    foreign = SubmissionStore(foreign_path).initialize()
    foreign.advance_clock(local_pending.deadline)
    foreign.begin_collection()
    foreign_pending = foreign.begin_collection()
    assert foreign_pending.publisher_generation == local_pending.publisher_generation
    assert foreign_pending.sequence == local_pending.sequence
    assert foreign_pending.deadline > local_pending.deadline
    reports = {GPU_1: "busy", GPU_2: "idle"}
    assert not publisher.publish(foreign_pending, reports)
    assert not publisher.publish(replace(local_pending, deadline=foreign_pending.deadline), reports)
    assert not publisher.publish(
        replace(local_pending, started_at=foreign_pending.started_at), reports
    )
    assert committed_admission(database) == expired
    assert publisher.publish(local_pending, reports)
    assert_owner_admission(client, database, headers, receipt, "waiting_dispatch", (GPU_2,))


def test_observation_clock_reserves_headroom_for_collection_deadline(client, database, queued_task):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    max_clock = 2**63 - 1
    latest_start = max_clock - OBSERVATION_TTL_SECONDS
    publisher.advance_clock(latest_start)
    token = publisher.begin_collection()
    assert token.started_at == latest_start
    assert token.deadline == max_clock
    assert publisher.publish(token, {GPU_1: "idle", GPU_2: "busy"})
    available, version = assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_1,)
    )

    publisher.advance_clock(latest_start + 29)
    almost, almost_version = assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_1,)
    )
    assert almost.clock == token.deadline - 1
    assert almost.admission_generation == available.admission_generation
    assert almost_version == version
    with pytest.raises(ValueError, match="无法记录下一次采集的截止点"):
        publisher.begin_collection()
    assert committed_admission(database) == (almost, almost_version)

    publisher.advance_clock(latest_start + 30)
    expired, expired_version = assert_owner_admission(
        client, database, headers, receipt, "gpu_observation_expired"
    )
    assert expired.clock == token.deadline
    assert expired.admission_generation == available.admission_generation + 1
    assert expired_version == version + 1
    publisher.advance_clock(token.deadline)
    publisher.advance_clock(token.deadline - 1)
    with pytest.raises(ValueError, match="SQLite 整数范围"):
        publisher.advance_clock(token.deadline + 1)
    assert committed_admission(database) == (expired, expired_version)


def test_observation_changes_candidates_without_changing_waiting_reason(
    client, database, queued_task
):
    headers, receipt = queued_task
    previous = None
    for reports, candidates in (
        ({GPU_1: "idle", GPU_2: "busy"}, (GPU_1,)),
        ({GPU_1: "busy", GPU_2: "idle"}, (GPU_2,)),
        ({GPU_1: "idle", GPU_2: "idle"}, (GPU_1, GPU_2)),
    ):
        token = publish(client, reports)
        current, version = assert_owner_admission(
            client, database, headers, receipt, "waiting_dispatch", candidates
        )
        assert current.published_sequence == token.sequence
        if previous is not None:
            assert (current.admission_generation, version) == previous
        previous = (current.admission_generation, version)


def test_observation_and_admission_generation_rollback_with_page_version(
    client, database, queued_task
):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    failure = publisher.begin_collection()
    available = assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_1,)
    )
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("""
            CREATE TRIGGER reject_observation_version
            BEFORE UPDATE OF snapshot_version ON queue_state
            BEGIN SELECT RAISE(ABORT, 'synthetic snapshot version failure'); END
            """)
    with pytest.raises(sqlite3.IntegrityError, match="synthetic snapshot version failure"):
        publisher.publish(failure, None)
    assert assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_1,)
    ) == available

    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("DROP TRIGGER reject_observation_version")
    assert publisher.publish(failure, None)
    blocked, version = assert_owner_admission(
        client, database, headers, receipt, "gpu_monitoring_failed"
    )
    assert blocked.admission_generation == available[0].admission_generation + 1
    assert version == available[1] + 1


@pytest.mark.parametrize("newer_arrives_before_retry", [False, True])
def test_observation_publication_retries_busy_writer(
    client, database, queued_task, monkeypatch, newer_arrives_before_retry
):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    failure = publisher.begin_collection()
    tokens_before, version = committed_admission(database)
    original_connect = sqlite3.connect
    beginnings = []
    newest = None
    with closing(original_connect(database, autocommit=True)) as blocker:
        blocker.execute("BEGIN IMMEDIATE")

        class ContendedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql != "BEGIN IMMEDIATE":
                    return super().execute(sql, parameters)
                beginnings.append(self)
                try:
                    return super().execute(sql, parameters)
                except sqlite3.OperationalError as error:
                    assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY
                    blocker.execute("ROLLBACK")
                    raise

        def connect(*args, **kwargs):
            nonlocal newest
            if newer_arrives_before_retry and beginnings:
                with monkeypatch.context() as patch:
                    patch.setattr(sqlite3, "connect", original_connect)
                    newest = publish(client, {GPU_1: "busy", GPU_2: "idle"})
            db = original_connect(*args, **kwargs, factory=ContendedConnection)
            db.execute("PRAGMA busy_timeout = 0")
            return db

        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", connect)
            assert publisher.publish(failure, None) is not newer_arrives_before_retry

    assert len(beginnings) == 2 and beginnings[0] is not beginnings[1]
    if newer_arrives_before_retry:
        current, current_version = assert_owner_admission(
            client, database, headers, receipt, "waiting_dispatch", (GPU_2,)
        )
        assert current.published_sequence == newest.sequence
        assert current.admission_generation == tokens_before.admission_generation
        assert current_version == version
        return

    blocked, current_version = assert_owner_admission(
        client, database, headers, receipt, "gpu_monitoring_failed"
    )
    assert blocked.published_sequence == failure.sequence
    assert blocked.admission_generation == tokens_before.admission_generation + 1
    assert current_version == version + 1


@pytest.mark.parametrize(
    "fault",
    [
        "committed", "closed_after_commit", "rolled_back", "open_transaction",
        "superseded", "same_token", "read_failure",
    ],
)
def test_observation_publish_reconciles_after_lost_commit(
    client, database, queued_task, monkeypatch, fault
):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    pending = publisher.begin_collection()
    newer = publisher.begin_collection()
    before, version = committed_admission(database)
    original_connect = sqlite3.connect
    connections = []
    evidence_reads = []

    class LostCommit(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql == "COMMIT":
                if fault in ("rolled_back", "same_token"):
                    super().execute("ROLLBACK")
                elif fault != "open_transaction":
                    super().execute("COMMIT")
                if fault in ("superseded", "same_token"):
                    competing = newer if fault == "superseded" else pending
                    with monkeypatch.context() as patch:
                        patch.setattr(sqlite3, "connect", original_connect)
                        assert publisher.publish(competing, {GPU_1: "unknown", GPU_2: "idle"})
                if fault == "closed_after_commit":
                    self.close()
                raise sqlite3.OperationalError("synthetic lost publication result")
            if "FROM gpu_admission_state WHERE" in sql and len(connections) > 1:
                evidence_reads.append(self)
                assert super().execute("PRAGMA query_only").fetchone()[0] == 1
                if fault == "read_failure":
                    raise sqlite3.OperationalError("synthetic unreadable publication evidence")
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        if connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                connections[0].execute("SELECT 1")
        db = original_connect(*args, **kwargs, factory=LostCommit)
        connections.append(db)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        if fault in ("superseded", "read_failure"):
            with pytest.raises(PublicationCommitUnknown) as unknown:
                publisher.publish(pending, None)
            if fault == "read_failure":
                assert str(unknown.value.__cause__) == "synthetic unreadable publication evidence"
        elif fault == "same_token":
            assert publisher.publish(pending, None) is False
        else:
            expected_commit = fault in ("committed", "closed_after_commit")
            assert publisher.publish(pending, None) is expected_commit

    if fault == "open_transaction":
        assert len(connections) == 1 and not evidence_reads
    else:
        assert len(connections) == 2
        assert evidence_reads == [connections[1]]

    if fault in ("rolled_back", "open_transaction"):
        assert assert_owner_admission(
            client, database, headers, receipt, "waiting_dispatch", (GPU_1,)
        ) == (before, version)
        return

    if fault in ("superseded", "same_token"):
        after, after_version = assert_owner_admission(
            client, database, headers, receipt, "gpu_visibility_unknown"
        )
        assert after.published_sequence == (newer if fault == "superseded" else pending).sequence
        assert after.admission_generation == before.admission_generation + 1
        assert after_version == version + (2 if fault == "superseded" else 1)
        return

    after, after_version = assert_owner_admission(
        client, database, headers, receipt, "gpu_monitoring_failed"
    )
    assert after.published_sequence == pending.sequence
    assert after.admission_generation == before.admission_generation + 1
    assert after_version == version + 1


@pytest.mark.parametrize("loss", ["missing_column", "missing_file"])
def test_observation_publish_preserves_inaccessible_evidence(tmp_path, monkeypatch, loss):
    path = tmp_path / "private" / "admission #&% evidence.sqlite3"
    publisher = SubmissionStore(path).initialize().replace_publisher()
    pending = publisher.begin_collection()
    original_connect = sqlite3.connect
    legacy_schema = None

    class LostCommit(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal legacy_schema
            if sql != "COMMIT":
                return super().execute(sql, parameters)
            super().execute("COMMIT")
            self.close()
            if loss == "missing_file":
                path.unlink()
            else:
                with closing(original_connect(path, autocommit=True)) as legacy:
                    legacy.execute(
                        "ALTER TABLE gpu_admission_state DROP COLUMN publication_attempt_id"
                    )
                    legacy_schema = legacy.execute(
                        "SELECT name, sql FROM sqlite_schema ORDER BY name"
                    ).fetchall()
            raise sqlite3.OperationalError("synthetic lost publication result")

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs, factory=LostCommit)
        if legacy_schema is not None:
            # 在应用设置 query_only 之前，文件打开模式本身就必须拒绝写入。
            with pytest.raises(sqlite3.OperationalError) as denied:
                db.execute("PRAGMA user_version = 1")
            assert denied.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_READONLY
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        with pytest.raises(PublicationCommitUnknown) as unknown:
            publisher.publish(pending, {GPU_1: "idle", GPU_2: "busy"})

    cause = unknown.value.__cause__
    assert isinstance(cause, sqlite3.OperationalError)
    if loss == "missing_file":
        assert cause.sqlite_errorcode & 0xFF == sqlite3.SQLITE_CANTOPEN
        assert not path.exists()
    else:
        assert "no such column: publication_attempt_id" in str(cause)
        assert legacy_schema is not None
        with closing(original_connect(path, autocommit=True)) as legacy:
            assert legacy.execute(
                "SELECT name, sql FROM sqlite_schema ORDER BY name"
            ).fetchall() == legacy_schema
            assert "publication_attempt_id" not in {
                column[1] for column in legacy.execute("PRAGMA table_info(gpu_admission_state)")
            }


def test_observation_restart_migrates_admission_without_publication_attempt_column(
    client, database, queued_task
):
    headers, receipt = queued_task
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    before, version = committed_admission(database)
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.execute("ALTER TABLE gpu_admission_state DROP COLUMN publication_attempt_id")

    replacement = SubmissionStore(database).initialize()
    after, after_version = assert_owner_admission(
        client, database, headers, receipt, "gpu_monitoring_failed"
    )
    assert after.admission_generation == before.admission_generation + 1
    assert after_version == version + 1
    token = replacement.begin_collection()
    assert replacement.publish(token, {GPU_1: "busy", GPU_2: "idle"})
    restored, restored_version = assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_2,)
    )
    assert restored.publication_attempt_id is not None
    assert restored_version == after_version + 1


@pytest.mark.parametrize(
    ("entry_point", "fault"),
    [
        ("replace", "lost_result"),
        ("replace", "closed_after_commit"),
        ("replace", "competing_rotation"),
        ("initialize", "closed_after_commit"),
    ],
)
def test_observation_publisher_reconciles_only_exact_rotation(
    client, database, queued_task, monkeypatch, entry_point, fault
):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    available, version = committed_admission(database)
    original_connect = sqlite3.connect
    connections = []
    competing_publishers = []
    rotate = (
        SubmissionStore(database).initialize
        if entry_point == "initialize"
        else publisher.replace_publisher
    )

    class LoseCommitResult(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql != "COMMIT":
                return super().execute(sql, parameters)
            if fault == "competing_rotation":
                super().execute("ROLLBACK")
                with monkeypatch.context() as patch:
                    patch.setattr(sqlite3, "connect", original_connect)
                    competing_publishers.append(SubmissionStore(database).initialize())
            else:
                super().execute("COMMIT")
                if fault == "closed_after_commit":
                    self.close()
            raise sqlite3.OperationalError("synthetic lost rotation result")

    def connect(*args, **kwargs):
        if connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                connections[0].execute("SELECT 1")
            assert kwargs.get("uri") is True
            assert args[0].endswith("?mode=ro")
        db = original_connect(*args, **kwargs, factory=LoseCommitResult)
        connections.append(db)
        return db

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", connect)
        if fault == "competing_rotation":
            with pytest.raises(sqlite3.OperationalError, match="synthetic lost rotation result"):
                rotate()
            replacement, = competing_publishers
        else:
            replacement = rotate()

    assert len(connections) == 2
    closed, closed_version = assert_owner_admission(
        client, database, headers, receipt, "gpu_monitoring_failed"
    )
    assert closed.publisher_generation == available.publisher_generation + 1
    assert closed.publisher_id != available.publisher_id
    assert closed.admission_generation == available.admission_generation + 1
    assert closed_version == version + 1
    with pytest.raises(StalePublisher):
        publisher.begin_collection()
    token = replacement.begin_collection()
    assert replacement.publish(token, {GPU_1: "busy", GPU_2: "idle"})
    assert_owner_admission(client, database, headers, receipt, "waiting_dispatch", (GPU_2,))


def test_observation_recovery_uses_latest_snapshot_and_rejects_old_publishers(
    client, database, queued_task
):
    headers, receipt = queued_task
    publisher = client.app.state.observations
    publish(client, {GPU_1: "idle", GPU_2: "busy"})
    assert_owner_admission(client, database, headers, receipt, "waiting_dispatch", (GPU_1,))
    publish(client, None)
    blocked, version = assert_owner_admission(
        client, database, headers, receipt, "gpu_monitoring_failed"
    )
    early = publisher.begin_collection()
    latest = publish(client, {GPU_1: "busy", GPU_2: "idle"})
    restored = assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_2,)
    )
    assert restored[0].admission_generation == blocked.admission_generation
    assert restored[0].published_sequence == latest.sequence > early.sequence
    assert restored[1] == version + 1
    for replay in (early, latest):
        assert not publisher.publish(replay, {GPU_1: "idle", GPU_2: "busy"})
        assert committed_admission(database) == restored

    pending = publisher.begin_collection()
    replacement = publisher.replace_publisher()
    closed, closed_version = assert_owner_admission(
        client, database, headers, receipt, "gpu_monitoring_failed"
    )
    assert closed.publisher_generation == blocked.publisher_generation + 1
    assert closed.admission_generation == blocked.admission_generation + 1
    assert closed_version == restored[1] + 1
    with pytest.raises(StalePublisher):
        publisher.begin_collection()

    current = replacement.begin_collection()
    assert current.sequence > pending.sequence
    assert replacement.publish(current, {GPU_1: "busy", GPU_2: "idle"})
    recovered = assert_owner_admission(
        client, database, headers, receipt, "waiting_dispatch", (GPU_2,)
    )
    assert not publisher.publish(pending, {GPU_1: "idle", GPU_2: "busy"})
    assert not replacement.publish(pending, {GPU_1: "idle", GPU_2: "busy"})
    assert committed_admission(database) == recovered


@pytest.mark.parametrize("clock_offset", [0, -30])
def test_cross_group_positions_follow_commit_order_after_reverse_receipts_and_restart(
    database, monkeypatch, clock_offset
):
    first_time = datetime(2026, 11, 25, tzinfo=UTC)
    times = iter([first_time, first_time + timedelta(seconds=clock_offset)])

    class SequenceClock:
        @staticmethod
        def now(tz):
            assert tz is UTC
            return next(times)

    first_committed = Barrier(2, timeout=10)
    release_first_receipt = Barrier(2, timeout=10)
    original_write = SubmissionStore._write

    def hold_first_receipt(store, account, submission):
        receipt = original_write(store, account, submission)
        if account.owner_id == "demo-a":
            first_committed.wait()
            release_first_receipt.wait()
        return receipt

    with (
        TestClient(create_app(database), base_url=BASE_URL) as first_client,
        TestClient(create_app(database), base_url=BASE_URL) as second_client,
    ):
        publish(second_client, {GPU_1: "busy", GPU_2: "busy"})
        first_headers = login(first_client)
        second_headers = login(second_client, "demo-b")
        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
            patch.setattr(submissions, "datetime", SequenceClock)
            patch.setattr(SubmissionStore, "_write", hold_first_receipt)
            first_pending = pool.submit(api_submit, first_client, first_headers, name="一组任务")
            first_committed.wait()
            try:
                second = api_submit(second_client, second_headers, name="二组任务")
                assert not first_pending.done()
            finally:
                release_first_receipt.wait()
            first = first_pending.result(timeout=10)

        assert datetime.fromisoformat(first["queued_at"]) == first_time
        assert datetime.fromisoformat(second["queued_at"]) == first_time + timedelta(
            seconds=clock_offset
        )
        assert (first["enqueue_seq"], second["enqueue_seq"]) == (1, 2)
        originals = []
        before = state(database)
        for actor, headers, receipt, position in (
            (first_client, first_headers, first, 1),
            (second_client, second_headers, second, 2),
        ):
            response = actor.get("/api/tasks", headers=headers)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            snapshot = response.json()
            assert snapshot["snapshot_version"] == "3"
            task, = snapshot["tasks"]
            assert (task["task_id"], task["enqueue_seq"]) == (receipt["task_id"], str(position))
            assert (task["queue_position"], task["waiting_reason"], task["can_cancel"]) == (
                position, "gpu_busy", False
            )
            detail = actor.get(f"/api/tasks/{receipt['task_id']}", headers=headers).json()
            assert detail == {"snapshot_version": "3", "task": task}
            originals.append(task)
        assert state(database) == before

    old_admission, _ = committed_admission(database)
    with TestClient(create_app(database), base_url=BASE_URL) as restarted:
        fresh_admission, version = committed_admission(database)
        assert fresh_admission.candidate_uuids == ()
        assert fresh_admission.publisher_generation == old_admission.publisher_generation + 1
        assert fresh_admission.admission_generation == old_admission.admission_generation + 1
        assert fresh_admission.waiting_reason == "gpu_monitoring_failed"
        assert version == 4
        originals = [{**task, "waiting_reason": "gpu_monitoring_failed"} for task in originals]
        headers = login(restarted)
        assert restarted.get("/api/tasks", headers=headers).json() == {
            "snapshot_version": "4", "tasks": [originals[0]],
        }
        headers = login(restarted, "demo-b")
        assert restarted.get("/api/tasks", headers=headers).json() == {
            "snapshot_version": "4", "tasks": [originals[1]],
        }
        third = api_submit(restarted, headers, name="重启后入队")
        assert third["enqueue_seq"] == 3
        snapshot = restarted.get("/api/tasks", headers=headers).json()
        assert snapshot["snapshot_version"] == "5"
        original, latest = snapshot["tasks"]
        assert original == originals[1]
        assert (latest["task_id"], latest["queue_position"], latest["waiting_reason"]) == (
            third["task_id"], 3, "gpu_monitoring_failed"
        )


def test_task_pages_and_api_are_owner_scoped(client):
    headers = login(client)
    name = "private-task-name-marker"
    receipt = api_submit(client, headers, name=name)
    path = f"/api/tasks/{receipt['task_id']}"
    html_path = f"/tasks/{receipt['task_id']}"
    assert 'href="/tasks">我的任务' in client.get("/").text
    assert 'id="tasks-page"' in client.get("/tasks").text
    detail_page = client.get(html_path).text
    assert 'id="tasks-page"' in detail_page
    assert f'data-task-id="{receipt["task_id"]}"' in detail_page
    assert '<script src="/static/tasks.js" defer></script>' in detail_page
    assert 'id="submission"' not in client.get("/tasks").text
    assert name not in detail_page
    assert client.get("/static/tasks.js").status_code == 200
    assert client.get(path, headers=headers).json()["task"]["name"] == name
    assert client.delete("/api/session", headers=headers).status_code == 204

    assert 'id="login"' in client.get(html_path).text
    assert 'id="tasks-page"' not in client.get(html_path).text
    for api_path in ("/api/tasks", path):
        denied = client.get(api_path)
        assert denied.status_code == 401 and set(denied.json()) == {"detail"}
    second_headers = login(client, "demo-b")
    listing = client.get("/api/tasks", headers=second_headers)
    assert listing.json() == {"snapshot_version": "1", "tasks": []}
    foreign = client.get(path, headers=second_headers)
    missing = client.get(f"/api/tasks/{uuid4()}", headers=second_headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json() == {"detail": "未找到该任务。", "snapshot_version": "1"}
    for api_path in ("/api/tasks", path):
        old_page = client.get(api_path, headers=headers)
        assert old_page.status_code == 403 and set(old_page.json()) == {"detail"}


def test_private_output_and_tasks_require_the_trusted_owner(client, database):
    name = "synthetic-name-13"
    command = "synthetic-command-13"
    output_text = "synthetic-path-13\nsynthetic-env-13\nsynthetic-log-13"
    markers = (name, command, *output_text.splitlines())
    owner = login(client)
    initial_scope = client.cookies[VIEW_SCOPE_COOKIE]
    task_id = api_submit(client, owner, name=name, command=command)["task_id"]
    detail = f"/api/tasks/{task_id}"
    without_output = client.get(detail, headers=owner).json()
    assert "output_id" not in without_output
    output_id = str(uuid4())
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("INSERT INTO task_outputs VALUES (?, ?, ?)", (output_id, task_id, output_text))
    output = f"{detail}/outputs/{output_id}"
    missing_task = f"/api/tasks/{uuid4()}"
    missing_output = f"{detail}/outputs/{uuid4()}"
    with_output = client.get(detail, headers=owner).json()
    assert with_output["output_id"] == output_id
    assert int(with_output["snapshot_version"]) == int(without_output["snapshot_version"]) + 1
    assert with_output["task"] == without_output["task"]
    assert client.get(output, headers=owner).json() == {"text": output_text}
    assert client.get(output, headers=owner).headers["Cache-Control"] == "no-store"
    assert client.get(missing_output, headers=owner).status_code == 404
    another_id = api_submit(client, owner)["task_id"]
    swapped = client.get(f"/api/tasks/{another_id}/outputs/{output_id}", headers=owner)
    assert swapped.status_code == 404
    assert swapped.json() == client.get(missing_output, headers=owner).json()

    before = state(database)
    forged_fields = {"owner_id": "demo-a", "group_id": "group-a", "task_id": task_id}
    for account in ("demo-a-peer", "demo-b", None):
        if account is None:
            client.cookies.clear()
            authenticated = {}
        else:
            authenticated = login(client, account)
            assert client.cookies[VIEW_SCOPE_COOKIE] != initial_scope
            group = "group-a" if account == "demo-a-peer" else "group-b"
            assert f"{account} · {group}" in client.get("/tasks").text
        for forged in (False, True):
            headers = dict(authenticated)
            if forged:
                headers.update({"X-Owner-Id": "demo-a", "X-Group-Id": "group-a"})
            for path in ("/api/tasks", detail, output):
                response = client.get(path, params=forged_fields if forged else {}, headers=headers)
                if account is None:
                    assert response.status_code == 401
                elif path == "/api/tasks":
                    assert response.status_code == 200
                    assert response.json()["tasks"] == []
                else:
                    assert response.status_code == 404
                    absent_path = missing_task if path == detail else missing_output
                    assert response.json() == client.get(absent_path, headers=headers).json()
                assert response.headers["Cache-Control"] == "no-store"
                assert all(marker not in response.text for marker in markers)
                assert output_id not in response.text
        # 可读的页面范围 Cookie 也不参与服务端授权。
        client.cookies.set(VIEW_SCOPE_COOKIE, initial_scope, domain="127.0.0.1", path="/")
        assert client.get(output, headers=authenticated).status_code == (404 if account else 401)
        page = client.get(f"/tasks/{task_id}")
        assert all(marker not in page.text for marker in markers)
    assert state(database) == before


def test_view_scope_tracks_login_switch_and_logout(client):
    owner = login(client)
    scope = client.cookies[VIEW_SCOPE_COOKIE]
    assert f'data-snapshot-scope="{scope}"' in client.get("/tasks").text
    task_id = api_submit(client, owner)["task_id"]
    output = f"/api/tasks/{task_id}/outputs/{uuid4()}"
    new_session = login(client, "demo-b")
    assert client.cookies[VIEW_SCOPE_COOKIE] != scope
    assert client.get(output, headers=owner).status_code == 403
    assert client.delete("/api/session", headers=new_session).status_code == 204
    assert client.cookies.get(VIEW_SCOPE_COOKIE) is None


def test_cancel_authorization_with_transactional_test_double(database):
    app = create_app(database)
    store = SubmissionStore(database)
    mutations = []

    @app.post("/_test/cancel/{task_id}", status_code=204)
    def cancel_double(task_id: str, session: Annotated[Session, Depends(require_csrf)]):
        with closing(store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if not store.owns_task(db, session.account, task_id):
                raise HTTPException(404, "未找到该任务。")
            mutations.append(task_id)
            db.execute("UPDATE tasks SET status = 'test-double' WHERE task_id = ?", (task_id,))
            db.execute("""
                UPDATE queue_state SET snapshot_version = snapshot_version + 1 WHERE id = 1
                """)
            db.execute("DELETE FROM test_leases WHERE task_id = ?", (task_id,))
            db.execute("COMMIT")

    def leases():
        with closing(sqlite3.connect(database)) as db:
            return db.execute("SELECT * FROM test_leases").fetchall()

    with TestClient(app, base_url=BASE_URL) as client:
        owner = login(client)
        task_id = api_submit(client, owner)["task_id"]
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TABLE test_leases (gpu_id TEXT, task_id TEXT)")
            db.execute("INSERT INTO test_leases VALUES ('synthetic-gpu-1', ?)", (task_id,))
        path = f"/_test/cancel/{task_id}"
        assert client.post(f"/api/tasks/{task_id}/cancel", headers=owner).status_code == 404
        before = state(database), leases()
        assert client.post(path).status_code == 403
        assert client.post(path, headers={"X-CSRF-Token": "wrong"}).status_code == 403

        for account in ("demo-a-peer", "demo-b", None):
            if account is None:
                client.cookies.clear()
                headers = {}
            else:
                headers = login(client, account)
            for claimed_identity in ({}, {"owner_id": "demo-a", "group_id": "group-a"}):
                denied = client.post(path, headers=headers, json=claimed_identity)
                assert denied.status_code == (401 if account is None else 404)
                assert denied.json() == {
                    "detail": "请先登录合成账号。" if account is None else "未找到该任务。"
                }
        assert mutations == []
        assert (state(database), leases()) == before
        assert client.post(path, headers=login(client)).status_code == 204
        assert mutations == [task_id]
        (task,), (sequence, version) = state(database)
        assert task[-2:] == ("test-double", sequence)
        assert (sequence, version) == (1, 2)
        assert leases() == []


@pytest.mark.parametrize("view", ["list", "detail", "foreign"])
@pytest.mark.parametrize("leave_queue", [False, True])
def test_task_snapshot_reads_version_position_and_reason_in_one_wal_snapshot(
    client, database, monkeypatch, view, leave_queue
):
    publish(client, {GPU_1: "busy", GPU_2: "busy"})
    first = api_submit(client, login(client))
    headers = login(client, "demo-b")
    second = api_submit(client, headers)
    second_path = f"/api/tasks/{second['task_id']}"
    first_path = f"/api/tasks/{first['task_id']}"
    path = {"list": "/api/tasks", "detail": second_path, "foreign": first_path}[view]
    original_task = client.get(second_path, headers=headers).json()["task"]
    assert (original_task["queue_position"], original_task["waiting_reason"]) == (2, "gpu_busy")
    _, old_version = committed_admission(database)
    started_read = Barrier(2, timeout=10)
    continue_read = Barrier(2, timeout=10)
    original_connect = sqlite3.connect

    class StagedRead(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if (
                "FROM gpu_admission_state WHERE" in sql
                and super().execute("PRAGMA query_only").fetchone()[0]
            ):
                assert self.in_transaction
                started_read.wait()
                continue_read.wait()
            return super().execute(sql, parameters)

    def connect(*args, **kwargs):
        return original_connect(*args, **kwargs, factory=StagedRead)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(sqlite3, "connect", connect)
        old_pending = pool.submit(client.get, path, headers=headers)
        started_read.wait()
        try:
            if leave_queue:
                with closing(original_connect(database, autocommit=True)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute(
                        "UPDATE tasks SET status = 'completed' WHERE task_id = ?",
                        (first["task_id"],),
                    )
                    writer.execute(
                        "UPDATE queue_state SET snapshot_version = snapshot_version + 1"
                    )
                    writer.execute("COMMIT")
            publish(client, {GPU_1: "unknown", GPU_2: "idle"})
        finally:
            continue_read.wait()
        old = old_pending.result(timeout=10)

    admission, version = committed_admission(database)
    assert admission.waiting_reason == "gpu_visibility_unknown"
    assert version == old_version + 1 + leave_queue
    if view == "foreign":
        assert old.status_code == 404
        assert old.json() == {"detail": "未找到该任务。", "snapshot_version": str(old_version)}
        current_absence = client.get(path, headers=headers)
        assert current_absence.status_code == 404
        assert current_absence.json() == {
            "detail": "未找到该任务。", "snapshot_version": str(version),
        }
    else:
        assert old.status_code == 200
        assert old.json()["snapshot_version"] == str(old_version)
        old_task = old.json()["task"] if view == "detail" else old.json()["tasks"][0]
        assert old_task == original_task
    before = state(database)
    new_list = client.get("/api/tasks", headers=headers).json()
    new_detail = client.get(second_path, headers=headers).json()
    new_task, = new_list["tasks"]
    assert new_detail == {"snapshot_version": str(version), "task": new_task}
    assert new_list["snapshot_version"] == str(version)
    assert new_task == {
        **original_task, "queue_position": 1 if leave_queue else 2,
        "waiting_reason": "gpu_visibility_unknown",
    }
    if leave_queue:
        first_headers = login(client)
        own_list = client.get("/api/tasks", headers=first_headers).json()
        left, = own_list["tasks"]
        assert (left["status"], left["queue_position"], left["waiting_reason"]) == (
            "completed", None, None
        )
        assert client.get(first_path, headers=first_headers).json() == {
            "snapshot_version": str(version), "task": left,
        }
    assert state(database) == before


@pytest.mark.parametrize("legacy_occupancy", [False, True])
def test_old_queue_migrates_without_using_legacy_occupancy(database, legacy_occupancy):
    with TestClient(create_app(database), base_url=BASE_URL) as previous:
        body = payload()
        receipt = previous.post("/api/submissions", json=body, headers=login(previous)).json()
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.executescript("DROP TABLE gpu_admission_state; DROP INDEX queued_by_sequence;")
        db.execute("UPDATE queue_state SET snapshot_version = 9007199254740993")
        if legacy_occupancy:
            db.executescript("""
                CREATE TABLE simulated_gpu_observations (
                    slot INTEGER PRIMARY KEY CHECK (slot IN (0, 1)),
                    occupancy TEXT NOT NULL CHECK (occupancy IN ('busy', 'unknown'))
                );
                INSERT INTO simulated_gpu_observations VALUES (0, 'busy'), (1, 'busy');
                """)

    expected_version = 9007199254740993 + legacy_occupancy
    with TestClient(create_app(database), base_url=BASE_URL) as migrated:
        closed, version = committed_admission(database)
        assert closed.waiting_reason == "gpu_monitoring_failed"
        assert closed.candidate_uuids == ()
        assert version == expected_version
        headers = login(migrated)
        prior = migrated.get(f"/api/submissions/{body['submission_key']}", headers=headers)
        assert prior.json() == receipt
        snapshot = migrated.get("/api/tasks", headers=headers).json()
        assert snapshot["snapshot_version"] == str(expected_version)
        task, = snapshot["tasks"]
        assert (task["task_id"], task["enqueue_seq"], task["queue_position"]) == (
            receipt["task_id"], str(receipt["enqueue_seq"]), 1,
        )
        assert task["waiting_reason"] == "gpu_monitoring_failed"
        path = f"/api/tasks/{task['task_id']}"
        assert migrated.get(path, headers=headers).json() == {
            "snapshot_version": str(expected_version), "task": task,
        }
        publisher = migrated.app.state.observations
        publisher.advance_clock(200)
        token = publish(migrated, {GPU_1: "idle", GPU_2: "busy"})
        pending = publisher.begin_collection()
        publisher.advance_clock(token.deadline)
        expired, version = committed_admission(database)
        assert expired.waiting_reason == "gpu_observation_expired"
        assert expired.candidate_uuids == ()
        assert version == expected_version + 2

    assert committed_admission(database) == (expired, version)
    with TestClient(create_app(database), base_url=BASE_URL) as restarted:
        closed, version = committed_admission(database)
        assert closed.waiting_reason == "gpu_monitoring_failed"
        assert closed.candidate_uuids == ()
        assert closed.admission_generation == expired.admission_generation + 1
        assert closed.publisher_generation == expired.publisher_generation + 1
        assert closed.clock == expired.clock
        assert version == expected_version + 3
        headers = login(restarted)
        assert restarted.get("/api/tasks", headers=headers).json() == {
            "snapshot_version": str(version), "tasks": [task],
        }
        assert restarted.get(path, headers=headers).json() == {
            "snapshot_version": str(version), "task": task,
        }
        assert not publisher.publish(pending, {GPU_1: "idle", GPU_2: "idle"})
        assert committed_admission(database) == (closed, version)
    if legacy_occupancy:
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("SELECT * FROM simulated_gpu_observations").fetchall() == [
                (0, "busy"), (1, "busy"),
            ]
