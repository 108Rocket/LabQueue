"""检查本地启动和 API 范围，使用临时数据库。"""

import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from labqueue.app import create_app


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("LabQueue requires Python 3.12")
    with (
        TemporaryDirectory() as temp,
        TestClient(create_app(Path(temp) / "check.sqlite3"), base_url="http://127.0.0.1") as client,
    ):
        health = client.get("/health")
        health.raise_for_status()
        payload = health.json()
        if payload.get("gpu_execution_enabled") is not False:
            raise RuntimeError("The local app must not enable GPU execution")
        if payload.get("gpu_monitoring") != "not-implemented":
            raise RuntimeError("The local app must not claim to have monitored a GPU")
        home = client.get("/")
        home.raise_for_status()
        if "LabQueue" not in home.text or '<html lang="zh-CN">' not in home.text:
            raise RuntimeError("The local homepage failed to render")
        schema = client.get("/openapi.json")
        schema.raise_for_status()
        writable = {
            (method, path)
            for path, operations in schema.json()["paths"].items()
            for method in operations
            if method in {"post", "put", "patch", "delete"}
        }
        expected = {
            ("post", "/api/session"),
            ("delete", "/api/session"),
            ("post", "/api/submissions"),
        }
        if writable != expected:
            raise RuntimeError(f"Unexpected writable API routes: {writable ^ expected}")
    print(f"PASS: Python {sys.version.split()[0]}, homepage, health and execution boundary")
    for package in ("fastapi", "jinja2", "sqlalchemy", "uvicorn", "httpx", "pytest", "ruff"):
        print(f"  {package}=={version(package)}")
    print("Scope: local startup check only; no GPU probe, job, training or benchmark was run.")


if __name__ == "__main__":
    main()
