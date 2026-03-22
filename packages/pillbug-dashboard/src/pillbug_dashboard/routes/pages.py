"""HTML page routes for the dashboard."""

import json
from pathlib import Path
from typing import cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pillbug_dashboard.services.runtime_hub import RuntimeHub

router = APIRouter(tags=["pages"])
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _runtime_hub(request: Request) -> RuntimeHub:
    return cast("RuntimeHub", request.app.state.runtime_hub)


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/runtimes", status_code=307)


@router.get("/runtimes", response_class=HTMLResponse)
async def runtimes(request: Request) -> HTMLResponse:
    snapshot = await _runtime_hub(request).build_overviews()
    return _TEMPLATES.TemplateResponse(
        request,
        "runtimes.html",
        {
            "page_title": "Runtimes",
            "initial_dashboard_json": json.dumps(snapshot.model_dump(mode="json")),
            "registry_path": str(request.app.state.settings.RUNTIME_REGISTRY_PATH),
        },
    )


@router.get("/runtimes/{runtime_id}", response_class=HTMLResponse)
async def runtime_detail(runtime_id: str, request: Request) -> HTMLResponse:
    try:
        snapshot = await _runtime_hub(request).build_detail(runtime_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _TEMPLATES.TemplateResponse(
        request,
        "runtime_detail.html",
        {
            "page_title": snapshot.registration.label or snapshot.registration.runtime_id,
            "initial_runtime_json": json.dumps(snapshot.model_dump(mode="json")),
        },
    )
