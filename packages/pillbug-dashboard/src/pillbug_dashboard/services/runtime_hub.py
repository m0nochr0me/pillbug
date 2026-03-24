"""Dashboard service that aggregates registry data with live runtime state."""

import asyncio
from typing import Any

from pillbug_dashboard.schema import (
    DashboardSummary,
    RegistryMutationResponse,
    RuntimeConnectionStatus,
    RuntimeDetailSnapshot,
    RuntimeOverview,
    RuntimeOverviewCollection,
    RuntimeRegistration,
    RuntimeRegistrationUpsert,
)
from pillbug_dashboard.services.registry import RegistryService
from pillbug_dashboard.services.runtime_client import RuntimeClient, RuntimeClientError


def _extract_a2a_runtime_id(destination: str) -> str | None:
    normalized_destination = destination.strip()
    if not normalized_destination:
        return None

    if normalized_destination.startswith("a2a:"):
        normalized_destination = normalized_destination[4:].strip()

    runtime_id, separator, _conversation_id = normalized_destination.partition("/")
    if not separator:
        return None

    normalized_runtime_id = runtime_id.strip()
    return normalized_runtime_id or None


def _extract_a2a_peers(channels_payload: dict[str, Any] | None) -> tuple[str, ...]:
    if channels_payload is None:
        return ()

    peers: set[str] = set()
    for channel in channels_payload.get("channels", []):
        if channel.get("name") != "a2a":
            continue

        details = channel.get("details")
        if isinstance(details, dict):
            configured_peers = details.get("configured_peers")
            if isinstance(configured_peers, list | tuple):
                normalized_peers = tuple(
                    peer.strip() for peer in configured_peers if isinstance(peer, str) and peer.strip()
                )
                if normalized_peers:
                    peers.update(normalized_peers)
                    continue

        for destination in channel.get("known_destinations", []):
            if not isinstance(destination, str):
                continue

            runtime_id = _extract_a2a_runtime_id(destination)
            if runtime_id:
                peers.add(runtime_id)

    return tuple(sorted(peers))


class RuntimeHub:
    def __init__(self, registry: RegistryService, runtime_client: RuntimeClient) -> None:
        self._registry = registry
        self._runtime_client = runtime_client

    def get_registration(self, runtime_id: str) -> RuntimeRegistration:
        return self._registry.get(runtime_id)

    def upsert_registration(self, payload: RuntimeRegistrationUpsert) -> RegistryMutationResponse:
        registration = self._registry.upsert(payload)
        return RegistryMutationResponse(
            message=f"Saved runtime {registration.runtime_id}.",
            registration=registration.to_public(),
        )

    def delete_registration(self, runtime_id: str) -> RegistryMutationResponse:
        registration = self._registry.delete(runtime_id)
        return RegistryMutationResponse(
            message=f"Removed runtime {registration.runtime_id}.",
            registration=registration.to_public(),
        )

    async def build_overviews(self) -> RuntimeOverviewCollection:
        registrations = self._registry.load().runtimes
        overviews = list(await asyncio.gather(*(self._build_overview(entry) for entry in registrations)))

        summary = DashboardSummary(total_runtimes=len(overviews))
        for overview in overviews:
            if overview.status.connected:
                summary.connected_runtimes += 1

            if overview.status.healthy:
                summary.healthy_runtimes += 1
            elif overview.status.connected:
                summary.degraded_runtimes += 1

            runtime_payload = overview.runtime or {}
            summary.active_sessions += int(runtime_payload.get("active_session_count", 0) or 0)

            tasks_payload = overview.tasks or {}
            if isinstance(tasks_payload, dict):
                scheduler_payload = tasks_payload.get("scheduler", {})
                summary.total_tasks += int(scheduler_payload.get("total_tasks", 0) or 0)
                summary.enabled_tasks += int(scheduler_payload.get("enabled_tasks", 0) or 0)

        return RuntimeOverviewCollection(summary=summary, runtimes=overviews)

    async def build_detail(self, runtime_id: str) -> RuntimeDetailSnapshot:
        registration = self._registry.get(runtime_id)
        token = registration.dashboard_bearer_token_value()

        results = await asyncio.gather(
            self._runtime_client.get_health(registration.base_url, token),
            self._runtime_client.get_runtime_telemetry(registration.base_url, token),
            self._runtime_client.get_channels_telemetry(registration.base_url, token),
            self._runtime_client.get_sessions_telemetry(registration.base_url, token),
            self._runtime_client.get_tasks_telemetry(registration.base_url, token),
            self._runtime_client.get_public_agent_card(registration.base_url),
            return_exceptions=True,
        )

        health, runtime, channels, sessions, tasks, agent_card = self._coerce_results(results)
        status = self._build_status(results, health)

        return RuntimeDetailSnapshot(
            registration=registration.to_public(),
            status=status,
            health=health,
            runtime=runtime,
            channels=channels,
            sessions=sessions,
            tasks=tasks,
            agent_card=agent_card,
            a2a_peers=_extract_a2a_peers(channels),
        )

    async def _build_overview(self, registration: RuntimeRegistration) -> RuntimeOverview:
        token = registration.dashboard_bearer_token_value()
        results = await asyncio.gather(
            self._runtime_client.get_health(registration.base_url, token),
            self._runtime_client.get_runtime_telemetry(registration.base_url, token),
            self._runtime_client.get_channels_telemetry(registration.base_url, token),
            self._runtime_client.get_tasks_telemetry(registration.base_url, token),
            return_exceptions=True,
        )

        health, runtime, channels, tasks = self._coerce_results(results)
        status = self._build_status(results, health)
        return RuntimeOverview(
            registration=registration.to_public(),
            status=status,
            health=health,
            runtime=runtime,
            channels=channels,
            tasks=tasks,
            a2a_peers=_extract_a2a_peers(channels),
        )

    def _coerce_results(self, results: list[Any]) -> list[dict[str, Any] | None]:
        payloads: list[dict[str, Any] | None] = []
        for result in results:
            payloads.append(result if isinstance(result, dict) else None)
        return payloads

    def _build_status(self, results: list[Any], health: dict[str, Any] | None) -> RuntimeConnectionStatus:
        first_error = next((result for result in results if isinstance(result, Exception)), None)
        if first_error is None:
            return RuntimeConnectionStatus(connected=True, healthy=(health or {}).get("status") == "ok")

        status_code = first_error.status_code if isinstance(first_error, RuntimeClientError) else None
        return RuntimeConnectionStatus(
            connected=any(isinstance(result, dict) for result in results),
            healthy=(health or {}).get("status") == "ok" if health else None,
            error=str(first_error),
            status_code=status_code,
        )
