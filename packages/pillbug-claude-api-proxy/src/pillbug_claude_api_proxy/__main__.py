"""Console entrypoint that runs the proxy under uvicorn."""

from __future__ import annotations

import uvicorn

from pillbug_claude_api_proxy.config import settings


def entrypoint() -> None:
    uvicorn.run(
        "pillbug_claude_api_proxy.app:app",
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
    )


if __name__ == "__main__":
    entrypoint()
