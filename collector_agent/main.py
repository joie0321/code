"""Local-only setup service for the customer-side collector appliance."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from collector_agent.client import ControlPlaneClientError
from collector_agent.config import AgentSettings
from collector_agent.runtime import AgentRuntime
from collector_agent.schemas import (
    AwsCredentialsRequest,
    AwsFlowLogSettingsRequest,
    AzureCredentialsRequest,
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

    app = FastAPI(title="Migration Discovery Collector", version="0.1.0", lifespan=lifespan)

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

    @app.delete("/api/v1/setup/sources/{collector_id}")
    def unregister(collector_id: str) -> dict[str, str]:
        try:
            return active_runtime.unregister(collector_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/setup/credentials")
    def configure_credentials(payload: LocalCredentialsRequest) -> dict[str, str]:
        try:
            active_runtime.configure_credentials(payload)
            return {"status": "stored_in_memory"}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/setup/azure/credentials")
    def configure_azure_credentials(payload: AzureCredentialsRequest) -> dict[str, str]:
        try:
            active_runtime.configure_azure_credentials(payload)
            return {"status": "stored_in_memory"}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/setup/aws/credentials")
    def configure_aws_credentials(payload: AwsCredentialsRequest) -> dict[str, str]:
        try:
            active_runtime.configure_aws_credentials(payload)
            return {"status": "stored_in_memory"}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/setup/aws/flow-logs")
    def configure_aws_flow_logs(payload: AwsFlowLogSettingsRequest) -> dict[str, str]:
        try:
            active_runtime.configure_aws_flow_logs(payload)
            return {"status": "stored_in_memory"}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/collection/inventory/sync")
    def sync_inventory(collector_id: str) -> dict[str, object]:
        try:
            return active_runtime.sync_inventory(collector_id)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/aws/flow-logs/sync")
    def sync_aws_flow_logs(collector_id: str) -> dict[str, int]:
        try:
            return active_runtime.sync_aws_flow_logs(collector_id)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/telemetry/observations")
    def send_observations(collector_id: str, payload: list[LocalObservation]) -> dict[str, object]:
        try:
            observations = [item.model_dump() for item in payload]
            return active_runtime.upload_observations(collector_id, observations)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/v1/ipfix/setup")
    def ipfix_setup(collector_id: str) -> dict[str, object]:
        try:
            return active_runtime.ipfix_setup(collector_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/ipfix/start")
    def start_ipfix(payload: IpfixStartRequest) -> dict[str, object]:
        try:
            return active_runtime.start_ipfix(payload.collector_id, payload)
        except (ControlPlaneClientError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/v1/ipfix/stop")
    def stop_ipfix(collector_id: str) -> dict[str, object]:
        try:
            return active_runtime.stop_ipfix(collector_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return app
