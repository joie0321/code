"""Local-only setup service for the customer-side collector appliance."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from collector_agent.client import ControlPlaneClientError
from collector_agent.config import AgentSettings
from collector_agent.runtime import AgentRuntime
from collector_agent.schemas import (
    IpfixStartRequest,
    LocalCredentialsRequest,
    LocalObservation,
    ReconnectRequest,
    RegistrationRequest,
)


def create_app(
    settings: AgentSettings | None = None, runtime: AgentRuntime | None = None
) -> FastAPI:
    """Create a loopback-only setup and status API for the collector appliance."""

    configured = settings or AgentSettings()
    active_runtime = runtime or AgentRuntime(configured)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        active_runtime.shutdown()

    app = FastAPI(title="VMware Migration Collector Agent", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def agent_status() -> dict[str, object]:
        return active_runtime.status()

    @app.post("/api/v1/setup/register")
    def register(payload: RegistrationRequest) -> dict[str, str]:
        try:
            return active_runtime.register(payload)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/setup/reconnect")
    def reconnect(payload: ReconnectRequest) -> dict[str, str]:
        try:
            return active_runtime.reconnect(payload)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/setup/credentials")
    def configure_credentials(payload: LocalCredentialsRequest) -> dict[str, str]:
        try:
            active_runtime.configure_credentials(payload)
            return {"status": "stored_in_memory"}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/collection/inventory/sync")
    def sync_inventory() -> dict[str, object]:
        try:
            return active_runtime.sync_inventory()
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/telemetry/observations")
    def send_observations(payload: list[LocalObservation]) -> dict[str, object]:
        try:
            observations = [item.model_dump() for item in payload]
            return active_runtime.upload_observations(observations)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/ipfix/setup")
    def ipfix_setup() -> dict[str, object]:
        return active_runtime.ipfix_setup()

    @app.post("/api/v1/ipfix/start")
    def start_ipfix(payload: IpfixStartRequest) -> dict[str, object]:
        try:
            return active_runtime.start_ipfix(payload)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/ipfix/stop")
    def stop_ipfix() -> dict[str, object]:
        return active_runtime.stop_ipfix()

    return app
