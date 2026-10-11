"""Local startup scaffold. GPU monitoring and task execution are not implemented."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

app = FastAPI(title="LabQueue", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "stage": "environment-scaffold",
        "gpu_monitoring": "not-implemented",
        "gpu_execution_enabled": False,
    }
