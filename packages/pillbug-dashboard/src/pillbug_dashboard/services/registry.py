"""Runtime registry loader for dashboard-managed nodes."""

import json
from pathlib import Path

from pillbug_dashboard.schema import RuntimeRegistration, RuntimeRegistrationUpsert, RuntimeRegistrySnapshot


class RegistryService:
    def __init__(self, registry_path: Path) -> None:
        self._registry_path = registry_path

    def load(self) -> RuntimeRegistrySnapshot:
        if not self._registry_path.is_file():
            return RuntimeRegistrySnapshot()

        payload = json.loads(self._registry_path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            payload = {"runtimes": payload}

        snapshot = RuntimeRegistrySnapshot.model_validate(payload)
        snapshot.runtimes.sort(key=lambda runtime: runtime.runtime_id.lower())
        return snapshot

    def get(self, runtime_id: str) -> RuntimeRegistration:
        normalized_runtime_id = runtime_id.strip()
        for runtime in self.load().runtimes:
            if runtime.runtime_id == normalized_runtime_id:
                return runtime

        raise KeyError(f"Runtime is not registered: {normalized_runtime_id}")

    def upsert(self, payload: RuntimeRegistrationUpsert) -> RuntimeRegistration:
        snapshot = self.load()
        runtimes = [runtime for runtime in snapshot.runtimes if runtime.runtime_id != payload.runtime_id]

        existing_registration = next(
            (runtime for runtime in snapshot.runtimes if runtime.runtime_id == payload.runtime_id),
            None,
        )

        if payload.clear_dashboard_bearer_token:
            dashboard_bearer_token = None
        elif payload.dashboard_bearer_token is not None:
            dashboard_bearer_token = payload.dashboard_bearer_token
        elif existing_registration is not None:
            dashboard_bearer_token = existing_registration.dashboard_bearer_token_value()
        else:
            dashboard_bearer_token = None

        registration = RuntimeRegistration(
            runtime_id=payload.runtime_id,
            base_url=payload.base_url,
            label=payload.label,
            dashboard_bearer_token=dashboard_bearer_token,
        )
        runtimes.append(registration)
        self._write(RuntimeRegistrySnapshot(runtimes=sorted(runtimes, key=lambda runtime: runtime.runtime_id.lower())))
        return registration

    def delete(self, runtime_id: str) -> RuntimeRegistration:
        snapshot = self.load()
        normalized_runtime_id = runtime_id.strip()
        kept_runtimes: list[RuntimeRegistration] = []
        removed_runtime: RuntimeRegistration | None = None

        for runtime in snapshot.runtimes:
            if runtime.runtime_id == normalized_runtime_id:
                removed_runtime = runtime
                continue
            kept_runtimes.append(runtime)

        if removed_runtime is None:
            raise KeyError(f"Runtime is not registered: {normalized_runtime_id}")

        self._write(RuntimeRegistrySnapshot(runtimes=kept_runtimes))
        return removed_runtime

    def _write(self, snapshot: RuntimeRegistrySnapshot) -> None:
        payload = {
            "runtimes": [runtime.to_storage() for runtime in snapshot.runtimes],
        }
        self._registry_path.parent.mkdir(parents=True, exist_ok=True)
        self._registry_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
