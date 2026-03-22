"""HTTP client for talking to Pillbug runtimes."""

from typing import Any

import httpx


class RuntimeClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RuntimeClient:
    def __init__(self, timeout_seconds: float) -> None:
        self._timeout = timeout_seconds

    def build_headers(self, bearer_token: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        return headers

    async def get_health(self, base_url: str, bearer_token: str | None = None) -> dict[str, Any]:
        return await self._request_json("GET", base_url=base_url, path="/health", bearer_token=bearer_token)

    async def get_runtime_telemetry(self, base_url: str, bearer_token: str | None = None) -> dict[str, Any]:
        return await self._request_json("GET", base_url=base_url, path="/telemetry/runtime", bearer_token=bearer_token)

    async def get_channels_telemetry(self, base_url: str, bearer_token: str | None = None) -> dict[str, Any]:
        return await self._request_json("GET", base_url=base_url, path="/telemetry/channels", bearer_token=bearer_token)

    async def get_sessions_telemetry(self, base_url: str, bearer_token: str | None = None) -> dict[str, Any]:
        return await self._request_json("GET", base_url=base_url, path="/telemetry/sessions", bearer_token=bearer_token)

    async def get_tasks_telemetry(self, base_url: str, bearer_token: str | None = None) -> dict[str, Any]:
        return await self._request_json("GET", base_url=base_url, path="/telemetry/tasks", bearer_token=bearer_token)

    async def get_public_agent_card(self, base_url: str) -> dict[str, Any] | None:
        try:
            return await self._request_json(
                "GET",
                base_url=base_url,
                path="/.well-known/agent-card.json",
                bearer_token=None,
            )
        except RuntimeClientError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def post_control_action(
        self,
        *,
        base_url: str,
        path: str,
        bearer_token: str | None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request_json(
            "POST",
            base_url=base_url,
            path=path,
            bearer_token=bearer_token,
            payload=payload,
        )

    async def _request_json(
        self,
        method: str,
        *,
        base_url: str,
        path: str,
        bearer_token: str | None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = self.build_headers(bearer_token)

        try:
            async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=self._timeout) as client:
                response = await client.request(method, path, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise RuntimeClientError(f"Failed to reach runtime at {base_url}: {exc}") from exc

        if response.status_code >= 400:
            detail = self._extract_error_detail(response)
            raise RuntimeClientError(detail, status_code=response.status_code)

        return dict(response.json())

    def _extract_error_detail(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict) and payload.get("detail"):
            return str(payload["detail"])

        return f"Runtime request failed with HTTP {response.status_code}."
