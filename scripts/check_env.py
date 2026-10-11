"""Check local startup and the scaffold's non-execution boundary, without a GPU."""

import sys
from importlib.metadata import version

from fastapi.testclient import TestClient

from labqueue.app import app


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("LabQueue requires Python 3.12")
    with TestClient(app) as client:
        health = client.get("/health")
        health.raise_for_status()
        payload = health.json()
        if payload.get("gpu_execution_enabled") is not False:
            raise RuntimeError("The environment scaffold must not enable GPU execution")
        if payload.get("gpu_monitoring") != "not-implemented":
            raise RuntimeError("The scaffold must not claim to have monitored a GPU")
        home = client.get("/")
        home.raise_for_status()
        if "LabQueue" not in home.text or '<html lang="zh-CN">' not in home.text:
            raise RuntimeError("The local homepage failed to render")
        schema = client.get("/openapi.json")
        schema.raise_for_status()
        for path, operations in schema.json()["paths"].items():
            if any(method in operations for method in ("post", "put", "patch", "delete")):
                raise RuntimeError(f"Unexpected writable API in the environment scaffold: {path}")
    print(f"PASS: Python {sys.version.split()[0]}, homepage, health and execution boundary")
    for package in ("fastapi", "jinja2", "sqlalchemy", "uvicorn", "httpx", "pytest", "ruff"):
        print(f"  {package}=={version(package)}")
    print("Scope: local startup check only; no GPU probe, job, training or benchmark was run.")


if __name__ == "__main__":
    main()
