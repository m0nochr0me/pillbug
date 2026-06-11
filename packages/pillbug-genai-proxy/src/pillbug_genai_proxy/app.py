"""FastAPI surface for the Gemini-to-OpenAI proxy."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from loguru import logger

from pillbug_genai_proxy.config import settings
from pillbug_genai_proxy.translate import StreamChunkAssembler, gemini_request_to_openai, openai_response_to_gemini

__all__ = ("build_app",)


def _build_upstream_headers() -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if api_key := settings.upstream_api_key():
        headers["authorization"] = f"Bearer {api_key}"
    return headers


def _resolve_upstream_chat_completions_url() -> str:
    return f"{settings.UPSTREAM_URL}/chat/completions"


async def _parse_request_payload(request: Request) -> dict[str, Any]:
    try:
        payload: Any = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid JSON body: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request body must be a JSON object")

    return payload


async def _open_upstream_stream(client: httpx.AsyncClient, upstream_request: dict[str, Any]) -> httpx.Response:
    upstream_http_request = client.build_request(
        "POST",
        _resolve_upstream_chat_completions_url(),
        json=upstream_request,
        headers=_build_upstream_headers(),
    )
    try:
        return await client.send(upstream_http_request, stream=True)
    except httpx.HTTPError as exc:
        logger.exception(f"Upstream streaming request failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"upstream request failed: {exc}",
        ) from exc


async def _gemini_sse(upstream_response: httpx.Response) -> AsyncIterator[str]:
    assembler = StreamChunkAssembler()
    finalized = False
    try:
        async for line in upstream_response.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                finalized = True
                for chunk in assembler.finalize():
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                break
            try:
                parsed = json.loads(data)
            except ValueError:
                logger.warning(f"Skipping malformed upstream SSE chunk: {data!r}")
                continue
            for chunk in assembler.handle_chunk(parsed):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        if not finalized:
            # Upstream closed without [DONE]; flush what we have so the turn
            # still carries a finishReason.
            for chunk in assembler.finalize():
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    except Exception as exc:
        # The 200 status is already committed; ending the stream without a
        # finishReason makes google-genai treat the turn as invalid, which is
        # the correct signal for a broken upstream stream.
        logger.exception(f"Upstream stream failed mid-turn: {exc}")
    finally:
        await upstream_response.aclose()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    timeout = httpx.Timeout(settings.REQUEST_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, verify=settings.UPSTREAM_VERIFY_TLS) as client:
        app.state.upstream_client = client
        logger.info(
            f"Gemini proxy ready: upstream={settings.UPSTREAM_URL} "
            f"model_override={settings.UPSTREAM_MODEL or '<pass-through>'}"
        )
        yield


def build_app() -> FastAPI:
    app = FastAPI(title="pillbug-genai-proxy", version="0.1.0", lifespan=_lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1beta/models/{model:path}:generateContent")
    @app.post("/v1/models/{model:path}:generateContent")
    async def generate_content(model: str, request: Request) -> JSONResponse:
        try:
            payload: Any = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid JSON body: {exc}") from exc

        if not isinstance(payload, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request body must be a JSON object")

        upstream_request = gemini_request_to_openai(
            payload,
            model_override=settings.UPSTREAM_MODEL,
            default_model=model,
        )

        client: httpx.AsyncClient = request.app.state.upstream_client
        try:
            upstream_response = await client.post(
                _resolve_upstream_chat_completions_url(),
                json=upstream_request,
                headers=_build_upstream_headers(),
            )
        except httpx.HTTPError as exc:
            logger.exception(f"Upstream request failed: {exc}")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"upstream request failed: {exc}",
            ) from exc

        if upstream_response.status_code >= 400:
            try:
                upstream_body = upstream_response.json()
            except ValueError:
                upstream_body = upstream_response.text
            logger.warning(f"Upstream returned status={upstream_response.status_code} body={upstream_body!r}")
            return JSONResponse(
                status_code=upstream_response.status_code,
                content={"error": {"code": upstream_response.status_code, "message": upstream_body}},
            )

        try:
            upstream_payload = upstream_response.json()
        except ValueError as exc:
            logger.exception("Upstream response was not valid JSON")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"upstream response was not valid JSON: {exc}",
            ) from exc

        return JSONResponse(content=openai_response_to_gemini(upstream_payload))

    @app.post("/v1beta/models/{model:path}:streamGenerateContent")
    @app.post("/v1/models/{model:path}:streamGenerateContent")
    async def stream_generate_content(model: str, request: Request) -> Response:
        payload = await _parse_request_payload(request)

        upstream_request = gemini_request_to_openai(
            payload,
            model_override=settings.UPSTREAM_MODEL,
            default_model=model,
        )
        upstream_request["stream"] = True
        # Spec-compliant OpenAI upstreams report token usage on the final stream chunk
        # with this option; servers that don't know it ignore unknown fields.
        upstream_request["stream_options"] = {"include_usage": True}

        client: httpx.AsyncClient = request.app.state.upstream_client
        upstream_response = await _open_upstream_stream(client, upstream_request)

        if upstream_response.status_code >= 400:
            error_body = (await upstream_response.aread()).decode("utf-8", errors="replace")
            await upstream_response.aclose()
            logger.warning(f"Upstream returned status={upstream_response.status_code} body={error_body!r}")
            return JSONResponse(
                status_code=upstream_response.status_code,
                content={"error": {"code": upstream_response.status_code, "message": error_body}},
            )

        return StreamingResponse(_gemini_sse(upstream_response), media_type="text/event-stream")

    @app.post("/upload/v1beta/files")
    @app.post("/upload/v1/files")
    async def upload_files() -> PlainTextResponse:
        return PlainTextResponse(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            content=(
                "file uploads are not supported by pillbug-genai-proxy; "
                "send attachments inline as Part.inline_data instead"
            ),
        )

    return app


app = build_app()
