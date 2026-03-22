"""FastAPI application factory for the dashboard."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from pillbug_dashboard.config import settings
from pillbug_dashboard.routes.api import router as api_router
from pillbug_dashboard.routes.pages import router as pages_router
from pillbug_dashboard.services.registry import RegistryService
from pillbug_dashboard.services.runtime_client import RuntimeClient
from pillbug_dashboard.services.runtime_hub import RuntimeHub


def create_app() -> FastAPI:
    package_root = Path(__file__).resolve().parent

    app = FastAPI(title=settings.APP_TITLE)
    app.state.settings = settings
    app.state.registry = RegistryService(settings.RUNTIME_REGISTRY_PATH)
    app.state.runtime_client = RuntimeClient(timeout_seconds=settings.RUNTIME_TIMEOUT_SECONDS)
    app.state.runtime_hub = RuntimeHub(app.state.registry, app.state.runtime_client)

    app.mount("/static", StaticFiles(directory=str(package_root / "static")), name="static")
    app.include_router(pages_router)
    app.include_router(api_router, prefix="/api")

    return app
