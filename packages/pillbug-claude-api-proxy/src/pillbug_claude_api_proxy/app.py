"""FastAPI surface for the Gemini-to-Anthropic-API proxy."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from loguru import logger

from pillbug_claude_api_proxy import audio, translate
from pillbug_claude_api_proxy.config import settings
from pillbug_claude_api_proxy.upstream import run_inference

__all__ = ("build_app",)


def build_app() -> FastAPI:
    app = FastAPI(title="pillbug-claude-api-proxy", version="0.1.0")

    logger.info(
        f"Claude-API proxy ready: model={settings.MODEL or '<from request URL>'} "
        f"max_tokens={settings.MAX_TOKENS} "
        f"oauth_token_set={bool(settings.resolved_oauth_token())}"
    )
    if not (settings.CLAUDE_CODE_SYSTEM_PREFIX or "").strip():
        logger.warning(
            "PB_CLAUDE_API_PROXY_CLAUDE_CODE_SYSTEM_PREFIX is empty — the Claude OAuth "
            "subscription path rejects requests without the Claude Code identity prefix "
            "with HTTP 429 rate_limit_error. Set it to "
            '"You are Claude Code, Anthropic\'s official CLI for Claude." or accept '
            "every call will fail."
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1beta/models/{model}:generateContent")
    @app.post("/v1/models/{model}:generateContent")
    async def generate_content(model: str, request: Request) -> JSONResponse:
        try:
            payload: Any = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid JSON body: {exc}") from exc

        if not isinstance(payload, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request body must be a JSON object")

        # Claude has no audio modality: rewrite inbound audio parts (transcribe or
        # placeholder) before history translation so they never hit the drop path.
        await audio.transcribe_inbound_audio(payload)

        system_text = translate.extract_system_text(payload)
        history = translate.extract_history(payload)
        tools = translate.extract_tool_decls(payload)
        generation_config = translate.extract_generation_config(payload)

        try:
            response_payload = await run_inference(
                system_text=system_text,
                history=history,
                tools=tools,
                generation_config=generation_config,
                model=model,
            )
        except Exception as exc:
            logger.exception(f"Anthropic upstream call failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"upstream Anthropic call failed: {exc}",
            ) from exc

        return JSONResponse(content=response_payload)

    @app.post("/v1beta/models/{model}:streamGenerateContent")
    @app.post("/v1/models/{model}:streamGenerateContent")
    async def stream_generate_content(model: str) -> PlainTextResponse:
        return PlainTextResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content=(
                "streamGenerateContent is not implemented by pillbug-claude-api-proxy; "
                "use the non-streaming generateContent endpoint instead"
            ),
        )

    @app.post("/upload/v1beta/files")
    @app.post("/upload/v1/files")
    async def upload_files() -> PlainTextResponse:
        return PlainTextResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content=(
                "file uploads are not supported by pillbug-claude-api-proxy; "
                "send attachments inline as Part.inline_data instead"
            ),
        )

    return app


app = build_app()
