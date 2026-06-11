"""FastAPI surface for the Gemini-to-Anthropic-API proxy."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from loguru import logger

from pillbug_claude_api_proxy import audio, translate
from pillbug_claude_api_proxy.config import settings
from pillbug_claude_api_proxy.upstream import run_inference, start_inference_stream

__all__ = ("build_app",)


async def _parse_request_payload(request: Request) -> dict[str, Any]:
    try:
        payload: Any = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid JSON body: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request body must be a JSON object")

    return payload


async def _gemini_sse(event_stream: Any) -> AsyncIterator[str]:
    assembler = translate.StreamChunkAssembler()
    try:
        async for event in event_stream:
            for chunk in assembler.handle_event(event):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    except Exception as exc:
        # The 200 status is already committed; ending the stream without a
        # finishReason makes google-genai treat the turn as invalid, which is
        # the correct signal for a broken upstream stream.
        logger.exception(f"Anthropic stream failed mid-turn: {exc}")


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
        payload = await _parse_request_payload(request)

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
    async def stream_generate_content(model: str, request: Request) -> StreamingResponse:
        payload = await _parse_request_payload(request)

        await audio.transcribe_inbound_audio(payload)

        system_text = translate.extract_system_text(payload)
        history = translate.extract_history(payload)
        tools = translate.extract_tool_decls(payload)
        generation_config = translate.extract_generation_config(payload)

        try:
            event_stream = await start_inference_stream(
                system_text=system_text,
                history=history,
                tools=tools,
                generation_config=generation_config,
                model=model,
            )
        except Exception as exc:
            logger.exception(f"Anthropic upstream streaming call failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"upstream Anthropic call failed: {exc}",
            ) from exc

        return StreamingResponse(_gemini_sse(event_stream), media_type="text/event-stream")

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
