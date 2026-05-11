"""Console entrypoint that runs the proxy under uvicorn."""

from __future__ import annotations

import uvicorn

from pillbug_genai_proxy.config import settings


def entrypoint() -> None:
    uvicorn.run(
        "pillbug_genai_proxy.app:app",
        host=settings.HOST,
        port=settings.PORT,
        log_config=None,
    )


if __name__ == "__main__":
    entrypoint()
