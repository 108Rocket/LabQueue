"""LabQueue 的本地模拟提交入口。"""

import json
import secrets
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import UUID4, BaseModel, ConfigDict, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from labqueue.auth import SESSION_COOKIE, SESSION_SECONDS, DemoSessions, Session
from labqueue.storage import default_database
from labqueue.submissions import (
    CommitUncertain,
    Receipt,
    Submission,
    SubmissionConflict,
    SubmissionStore,
)

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
FIELD_ERRORS = {
    "username": "请输入合成账号。",
    "password": "请输入合成账号的密码。",
    "submission_key": "请输入有效的原提交键。",
    "name": "请输入任务名（最多 120 个字符）。",
    "command": "请输入任务命令（最多 8192 个字符）。",
    "memory_gib": "请输入大于 0 的 GiB 数值，或选择未知。",
}


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=512)]


class SubmissionRequest(Request):
    async def json(self):
        return json.loads(await self.body(), parse_float=Decimal)


class SubmissionRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            return await handler(SubmissionRequest(request.scope, request.receive))

        return handle


def require_session(request: Request) -> Session:
    session = request.app.state.sessions.get(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(401, "请先登录合成账号。")
    return session


def require_csrf(
    request: Request, session: Annotated[Session, Depends(require_session)]
) -> Session:
    verify_page_session(request, session)
    return session


def verify_page_session(request: Request, session: Session) -> None:
    token = request.headers.get("X-CSRF-Token", "")
    if not secrets.compare_digest(token.encode(), session.csrf_token.encode()):
        raise HTTPException(403, "会话凭证已变更，请刷新页面。")


def create_app(database_path: Path | None = None) -> FastAPI:
    store = SubmissionStore(database_path or default_database())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if (
            database_path is None
            and not store.path.exists()
            and Path("data/labqueue.sqlite3").exists()
        ):
            raise RuntimeError("检测到旧任务库，请先停服并将 SQLite 备份迁入账号私有目录")
        store.initialize()
        app.state.sessions = DemoSessions()
        yield

    app = FastAPI(title="LabQueue", version="0.1.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.middleware("http")
    async def prevent_caching(request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        fields = {}
        for issue in error.errors():
            field = issue["loc"][-1]
            if field in FIELD_ERRORS and issue["type"] != "extra_forbidden":
                fields[field] = FIELD_ERRORS[field]
        return JSONResponse({"detail": "请检查请求内容。", "fields": fields}, status_code=422)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request) -> HTMLResponse:
        session = app.state.sessions.get(request.cookies.get(SESSION_COOKIE))
        return templates.TemplateResponse(
            request=request, name="index.html", context={"session": session}
        )

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        return {
            "status": "ok",
            "stage": "simulated-submission",
            "gpu_monitoring": "not-implemented",
            "gpu_execution_enabled": False,
        }

    @app.post("/api/session")
    def login(credentials: Credentials, request: Request, response: Response) -> dict[str, str]:
        # 跨站表单无法附加此请求头，跨站脚本也得不到 CORS 预检许可。
        if request.headers.get("X-LabQueue-Request") != "1":
            raise HTTPException(403, "请通过 LabQueue 登录。")
        result = app.state.sessions.login(credentials.username, credentials.password)
        if result is None:
            raise HTTPException(401, "合成账号或密码无效。")
        session_id, session = result
        app.state.sessions.logout(request.cookies.get(SESSION_COOKIE))
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            max_age=SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
        )
        return {"csrf_token": session.csrf_token}

    @app.delete("/api/session", dependencies=[Depends(require_csrf)], status_code=204)
    def logout(request: Request) -> Response:
        app.state.sessions.logout(request.cookies.get(SESSION_COOKIE))
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE)
        return response

    submission_router = APIRouter(route_class=SubmissionRoute)

    @submission_router.post("/api/submissions")
    def submit(
        submission: Submission, session: Annotated[Session, Depends(require_csrf)]
    ) -> Receipt:
        try:
            return store.submit(session.account, submission)
        except SubmissionConflict:
            raise HTTPException(409, "原提交键已用于不同内容。") from None
        except (sqlite3.Error, CommitUncertain):
            raise HTTPException(503, "暂未确认提交结果，请用原提交键核对。") from None

    app.include_router(submission_router)

    @app.get("/api/submissions/{submission_key}")
    def lookup(
        submission_key: UUID4,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> Receipt:
        if "X-CSRF-Token" in request.headers:
            verify_page_session(request, session)
        try:
            receipt = store.lookup(session.account, submission_key)
        except sqlite3.Error:
            raise HTTPException(503, "暂时无法核对，请保留原提交键。") from None
        if receipt is None:
            raise HTTPException(404, "当前未核对到该提交的回执。")
        return receipt

    return app


app = create_app()
