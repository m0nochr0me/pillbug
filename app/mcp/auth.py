"""
Bearer-token authorization, runtime auth metadata, and control-action auditing
for the composition MCP server's HTTP surfaces.
"""

import json
import secrets
from typing import Any, Literal

from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.log import logger
from app.core.telemetry import runtime_telemetry
from app.schema.control import AuthScope, AuthTokenBinding, OperatorResponse, RuntimeAuthConfiguration
from app.schema.telemetry import RuntimeMetadata


def _build_runtime_metadata() -> RuntimeMetadata:
    return runtime_telemetry.metadata()


def _build_runtime_auth_configuration() -> RuntimeAuthConfiguration:
    token_bindings: list[AuthTokenBinding] = []
    dashboard_token = settings.dashboard_bearer_token()
    a2a_token = settings.a2a_bearer_token()

    if dashboard_token is not None:
        token_bindings.append(
            AuthTokenBinding(
                token_name="dashboard-bearer",
                principal="dashboard",
                scopes=(AuthScope.TELEMETRY, AuthScope.CONTROL),
            )
        )

    if a2a_token is not None:
        token_bindings.append(
            AuthTokenBinding(
                token_name="a2a-bearer",
                principal="a2a",
                scopes=(AuthScope.A2A,),
            )
        )

    return RuntimeAuthConfiguration(
        token_bindings=tuple(token_bindings),
        telemetry_protected=dashboard_token is not None,
        control_protected=True,
        a2a_protected=a2a_token is not None,
    )


def _extract_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None

    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    return token.strip() or None


async def _authorize_telemetry(authorization: str | None) -> AuthScope | None:
    expected_token = settings.dashboard_bearer_token()
    if expected_token is None:
        return None

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.TELEMETRY


async def _authorize_control(authorization: str | None) -> AuthScope:
    expected_token = settings.dashboard_bearer_token()
    if expected_token is None:
        raise HTTPException(
            status_code=503,
            detail="Control API requires PB_DASHBOARD_BEARER_TOKEN to be configured.",
        )

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.CONTROL


async def _authorize_a2a(authorization: str | None) -> AuthScope:
    expected_token = settings.a2a_bearer_token()
    if expected_token is None:
        return AuthScope.A2A

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.A2A


def _ensure_a2a_discovery_available() -> None:
    if "a2a" not in settings.enabled_channels():
        raise HTTPException(status_code=404, detail="A2A discovery is not enabled on this runtime.")


def _operator_response(
    *,
    action: str,
    message: str,
    scope: AuthScope,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return OperatorResponse(
        runtime_id=settings.runtime_id,
        ok=True,
        action=action,
        message=message,
        scope=scope,
        details=details,
    ).model_dump(mode="json")


async def _audit_control_action(
    request: Request,
    *,
    action: str,
    scope: AuthScope,
    message: str,
    level: Literal["info", "warning", "error"] = "info",
    details: dict[str, Any] | None = None,
) -> None:
    payload = {
        "runtime_id": settings.runtime_id,
        "scope": scope.value,
        "action": action,
        "path": str(request.url.path),
        "client_host": request.client.host if request.client is not None else None,
        **{key: value for key, value in (details or {}).items() if value is not None},
    }
    rendered_payload = json.dumps(payload, sort_keys=True, default=str)

    if level == "error":
        logger.error(f"Control action {message} {rendered_payload}")
    elif level == "warning":
        logger.warning(f"Control action {message} {rendered_payload}")
    else:
        logger.info(f"Control action {message} {rendered_payload}")

    await runtime_telemetry.record_event(
        event_type=f"control.{action}",
        source="control-api",
        level=level,
        message=message,
        data=payload,
    )
