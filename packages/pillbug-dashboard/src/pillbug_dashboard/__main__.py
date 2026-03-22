"""Dashboard entrypoint."""

import argparse

import uvicorn

from pillbug_dashboard.app import create_app
from pillbug_dashboard.config import settings


def entrypoint() -> None:
    parser = argparse.ArgumentParser(description="Pillbug dashboard")
    parser.add_argument("--host", default=settings.HOST)
    parser.add_argument("--port", type=int, default=settings.PORT)
    args = parser.parse_args()

    settings.ensure_directories()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    entrypoint()
