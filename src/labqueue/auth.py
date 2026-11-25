"""本地模拟账号及由服务端验证的会话。"""

import os
import secrets
from dataclasses import dataclass
from threading import Lock
from time import monotonic

SESSION_COOKIE = "labqueue_session"
VIEW_SCOPE_COOKIE = "labqueue_view_scope"
SESSION_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class Account:
    owner_id: str
    group_id: str


@dataclass(frozen=True)
class Session:
    account: Account
    csrf_token: str
    expires_at: float
    snapshot_scope: str


class DemoSessions:
    def __init__(self) -> None:
        self._accounts = {
            "demo-a": (Account("demo-a", "group-a"), os.getenv("LABQUEUE_DEMO_A_PASSWORD")),
            "demo-a-peer": (
                Account("demo-a-peer", "group-a"), os.getenv("LABQUEUE_DEMO_A_PEER_PASSWORD")
            ),
            "demo-b": (Account("demo-b", "group-b"), os.getenv("LABQUEUE_DEMO_B_PASSWORD")),
        }
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()

    def login(self, username: str, password: str) -> tuple[str, Session] | None:
        account_and_password = self._accounts.get(username)
        if account_and_password is None:
            return None
        account, expected = account_and_password
        if not expected or not secrets.compare_digest(
            password.encode(errors="surrogatepass"), expected.encode(errors="surrogatepass")
        ):
            return None

        session_id = secrets.token_urlsafe(32)
        now = monotonic()
        session = Session(
            account, secrets.token_urlsafe(32), now + SESSION_SECONDS, secrets.token_urlsafe(16)
        )
        with self._lock:
            self._sessions = {
                key: session for key, session in self._sessions.items() if session.expires_at > now
            }
            self._sessions[session_id] = session
        return session_id, session

    def get(self, session_id: str | None) -> Session | None:
        if session_id is None:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None and session.expires_at > monotonic():
                return session
            self._sessions.pop(session_id, None)
        return None

    def logout(self, session_id: str | None) -> None:
        if session_id is not None:
            with self._lock:
                self._sessions.pop(session_id, None)
