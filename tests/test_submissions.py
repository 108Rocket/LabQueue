import json
import os
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import labqueue.submissions as submissions
from labqueue.app import create_app
from labqueue.storage import default_database, verify_storage
from labqueue.submissions import SubmissionStore

BASE_URL = "http://127.0.0.1"
ACCOUNT_PASSWORDS = {"demo-a": "synthetic-demo-a", "demo-b": "synthetic-demo-b"}


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
    # 双卡忙是模拟场景前提，登记任务不需要任何 GPU 观测输入。
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
        (1, 1),
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
            assert snapshot["snapshot_version"] == "2"
            task, = snapshot["tasks"]
            assert (task["task_id"], task["enqueue_seq"]) == (receipt["task_id"], str(position))
            assert (task["queue_position"], task["waiting_reason"], task["can_cancel"]) == (
                position, "gpu_busy", False
            )
            detail = actor.get(f"/api/tasks/{receipt['task_id']}", headers=headers).json()
            assert detail == {"snapshot_version": "2", "task": task}
            originals.append(task)
        assert state(database) == before

    with TestClient(create_app(database), base_url=BASE_URL) as restarted:
        headers = login(restarted)
        assert restarted.get("/api/tasks", headers=headers).json() == {
            "snapshot_version": "2", "tasks": [originals[0]],
        }
        headers = login(restarted, "demo-b")
        assert restarted.get("/api/tasks", headers=headers).json() == {
            "snapshot_version": "2", "tasks": [originals[1]],
        }
        third = api_submit(restarted, headers, name="重启后入队")
        assert third["enqueue_seq"] == 3
        snapshot = restarted.get("/api/tasks", headers=headers).json()
        assert snapshot["snapshot_version"] == "3"
        original, latest = snapshot["tasks"]
        assert original == originals[1]
        assert (latest["task_id"], latest["queue_position"], latest["waiting_reason"]) == (
            third["task_id"], 3, "gpu_busy"
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


@pytest.mark.parametrize("view", ["list", "detail", "foreign"])
@pytest.mark.parametrize("leave_queue", [False, True])
def test_task_snapshot_reads_version_position_and_reason_in_one_wal_snapshot(
    client, database, monkeypatch, view, leave_queue
):
    first = api_submit(client, login(client))
    headers = login(client, "demo-b")
    second = api_submit(client, headers)
    second_path = f"/api/tasks/{second['task_id']}"
    first_path = f"/api/tasks/{first['task_id']}"
    path = {"list": "/api/tasks", "detail": second_path, "foreign": first_path}[view]
    original_task = client.get(second_path, headers=headers).json()["task"]
    assert (original_task["queue_position"], original_task["waiting_reason"]) == (2, "gpu_busy")
    started_read = Barrier(2, timeout=10)
    continue_read = Barrier(2, timeout=10)
    original_connect = sqlite3.connect

    class StagedRead(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "FROM simulated_gpu_observations WHERE" in sql:
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
            with closing(original_connect(database, autocommit=True)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                if leave_queue:
                    writer.execute(
                        "UPDATE tasks SET status = 'completed' WHERE task_id = ?",
                        (first["task_id"],),
                    )
                writer.execute("UPDATE simulated_gpu_observations SET occupancy = 'unknown'")
                writer.execute("UPDATE queue_state SET snapshot_version = snapshot_version + 1")
                writer.execute("COMMIT")
        finally:
            continue_read.wait()
        old = old_pending.result(timeout=10)

    if view == "foreign":
        assert old.status_code == 404
        assert old.json() == {"detail": "未找到该任务。", "snapshot_version": "2"}
        current_absence = client.get(path, headers=headers)
        assert current_absence.status_code == 404
        assert current_absence.json() == {"detail": "未找到该任务。", "snapshot_version": "3"}
    else:
        assert old.status_code == 200
        assert old.json()["snapshot_version"] == "2"
        old_task = old.json()["task"] if view == "detail" else old.json()["tasks"][0]
        assert old_task == original_task
    before = state(database)
    new_list = client.get("/api/tasks", headers=headers).json()
    new_detail = client.get(second_path, headers=headers).json()
    new_task, = new_list["tasks"]
    assert new_detail == {"snapshot_version": "3", "task": new_task}
    assert new_list["snapshot_version"] == "3"
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
            "snapshot_version": "3", "task": left,
        }
    assert state(database) == before


def test_task_read_model_migrates_story_1_1_db_and_preserves_simulation_on_restart(database):
    with TestClient(create_app(database), base_url=BASE_URL) as previous:
        body = payload()
        receipt = previous.post("/api/submissions", json=body, headers=login(previous)).json()
    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.executescript("DROP TABLE simulated_gpu_observations; DROP INDEX queued_by_sequence;")
        db.execute("UPDATE queue_state SET snapshot_version = 9007199254740993")

    with TestClient(create_app(database), base_url=BASE_URL) as migrated:
        headers = login(migrated)
        prior = migrated.get(f"/api/submissions/{body['submission_key']}", headers=headers)
        assert prior.json() == receipt
        snapshot = migrated.get("/api/tasks", headers=headers).json()
        assert snapshot["snapshot_version"] == "9007199254740993"
        assert snapshot["tasks"][0]["waiting_reason"] == "gpu_busy"

    with closing(sqlite3.connect(database, autocommit=True)) as db:
        db.executescript("""
            BEGIN IMMEDIATE;
            DELETE FROM simulated_gpu_observations WHERE slot = 0;
            UPDATE queue_state SET snapshot_version = snapshot_version + 1;
            COMMIT;
            """)
    with TestClient(create_app(database), base_url=BASE_URL) as restarted:
        headers = login(restarted)
        snapshot = restarted.get("/api/tasks", headers=headers).json()
        assert snapshot["snapshot_version"] == "9007199254740994"
        assert snapshot["tasks"][0]["waiting_reason"] == "gpu_visibility_unknown"
