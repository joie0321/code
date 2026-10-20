# ruff: noqa: E501
"""Loopback-only host for the separate React Control Plane dashboard.

The browser receives data only; control-plane dashboard and administrator keys
remain in this process.  This is deliberately an allow-listed façade, not a
general-purpose HTTP proxy.
"""

from __future__ import annotations

from pathlib import Path
from re import fullmatch
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi import Request as FastAPIRequest
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from control_plane.config import ControlPlaneSettings
from control_plane.dashboard_client import ControlPlaneDashboardClient, DashboardClientError

_DIST_DIRECTORY = Path(__file__).with_name("dashboard_react") / "dist"
_MAX_REQUEST_BODY_BYTES = 4 * 1024


def _client() -> ControlPlaneDashboardClient:
    settings = ControlPlaneSettings()
    settings.validate_runtime()
    return ControlPlaneDashboardClient(
        settings.control_plane_dashboard_api_url,
        settings.dashboard_api_key().get_secret_value(),
        settings.admin_api_key().get_secret_value(),
    )


def _collector_id(value: str) -> str:
    if fullmatch(r"[A-Za-z0-9-]{36}", value) is None:
        raise HTTPException(status_code=400, detail="Invalid collector ID")
    return value


async def _body(request: FastAPIRequest) -> dict[str, Any]:
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(status_code=415, detail="JSON request content is required")
    raw = await request.body()
    if not raw or len(raw) > _MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body is invalid or too large")
    try:
        import json

        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return value


def _api_error(error: DashboardClientError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(error))


def create_react_dashboard_app() -> FastAPI:
    """Create the local host for the limited Control Plane React dashboard."""

    app = FastAPI(title="Migration Discovery Control Plane React Dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/collectors")
    def collectors() -> list[dict[str, Any]]:
        try:
            return _client().collectors()
        except (DashboardClientError, ValueError) as error:
            raise _api_error(DashboardClientError("Control-plane dashboard request failed")) from error

    @app.get("/api/v1/collectors/{collector_id}/inventory")
    def inventory(
        collector_id: str,
        historical: bool = False,
        historical_as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        validated_collector_id = _collector_id(collector_id)
        try:
            return _client().inventory(validated_collector_id, historical, historical_as_of)
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.get("/api/v1/collectors/{collector_id}/connections")
    def connections(
        collector_id: str, observed_after: str | None = None, observed_before: str | None = None,
        traffic_batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        validated_collector_id = _collector_id(collector_id)
        try:
            return _client().connections(validated_collector_id, observed_after, observed_before, traffic_batch_id)
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.get("/api/v1/collectors/{collector_id}/waves")
    def waves(
        collector_id: str, observed_after: str | None = None, observed_before: str | None = None,
        historical: bool = False, traffic_batch_id: str | None = None,
    ) -> list[dict[str, Any]]:
        validated_collector_id = _collector_id(collector_id)
        try:
            return _client().waves(validated_collector_id, observed_after, observed_before, False, historical, traffic_batch_id)
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.get("/api/v1/collectors/{collector_id}/wave-summary")
    def wave_summary(
        collector_id: str, observed_after: str | None = None, observed_before: str | None = None,
        historical: bool = False, traffic_batch_id: str | None = None,
    ) -> dict[str, Any]:
        validated_collector_id = _collector_id(collector_id)
        try:
            return _client().wave_summary(validated_collector_id, observed_after, observed_before, False, historical, traffic_batch_id)
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.get("/api/v1/collectors/{collector_id}/current-environment")
    def current_environment(
        collector_id: str,
        observed_after: str | None = None,
        observed_before: str | None = None,
        historical: bool = False,
        traffic_batch_id: str | None = None,
        traffic_received_after: str | None = None,
    ) -> dict[str, Any]:
        validated_collector_id = _collector_id(collector_id)
        try:
            return _client().current_environment(
                validated_collector_id,
                observed_after,
                observed_before,
                False,
                historical,
                traffic_batch_id,
                traffic_received_after,
            )
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.get("/api/v1/collectors/{collector_id}/wave-connections")
    def wave_connections(
        collector_id: str, vm_uuid: list[str] = Query(default=[]),  # noqa: B008
        observed_after: str | None = None,
        observed_before: str | None = None, page: int = Query(default=1, ge=1, le=100000),
        exclude_dynamic_private_ports: bool = False,
        traffic_batch_id: str | None = None, traffic_received_after: str | None = None,
    ) -> dict[str, Any]:
        if len(vm_uuid) > 500:
            raise HTTPException(status_code=400, detail="Too many VM IDs")
        validated_collector_id = _collector_id(collector_id)
        try:
            return _client().wave_connections(
                validated_collector_id, vm_uuid, observed_after, observed_before, page,
                deduplicate=True, exclude_dynamic_private_ports=exclude_dynamic_private_ports,
                traffic_batch_id=traffic_batch_id, traffic_received_after=traffic_received_after,
            )
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.get("/api/v1/collectors/{collector_id}/wave-grouping-port-allowlist")
    def wave_grouping_port_allowlist(collector_id: str) -> dict[str, Any]:
        try:
            return _client().wave_grouping_port_allowlist(_collector_id(collector_id))
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.put("/api/v1/collectors/{collector_id}/wave-grouping-port-allowlist")
    async def update_wave_grouping_port_allowlist(
        collector_id: str, request: FastAPIRequest
    ) -> dict[str, Any]:
        payload = await _body(request)
        ports = payload.get("ports")
        if not isinstance(ports, list) or len(ports) > 128:
            raise HTTPException(status_code=400, detail="ports must be a list of at most 128 entries")
        for entry in ports:
            if (
                not isinstance(entry, dict)
                or entry.get("protocol") not in {"tcp", "udp"}
                or not isinstance(entry.get("port"), int)
                or not 32768 <= entry["port"] <= 65535
            ):
                raise HTTPException(
                    status_code=400,
                    detail="each port must use tcp or udp and be between 32768 and 65535",
                )
        try:
            return _client().update_wave_grouping_port_allowlist(
                _collector_id(collector_id), ports
            )
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.post("/api/v1/enrollments")
    async def enrollment(request: FastAPIRequest) -> dict[str, Any]:
        payload = await _body(request)
        tenant = payload.get("tenant_id")
        expiry = payload.get("expires_in_minutes")
        if not isinstance(tenant, str) or fullmatch(r"[A-Za-z0-9_-]{1,64}", tenant) is None:
            raise HTTPException(status_code=400, detail="Customer tenant ID must contain 1-64 letters, numbers, underscores, or hyphens")
        if expiry not in {15, 30, 60}:
            raise HTTPException(status_code=400, detail="Code expiry must be 15, 30, or 60 minutes")
        try:
            return _client().create_enrollment(tenant, expiry)
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.post("/api/v1/collectors/{collector_id}/reconnection-codes")
    async def reconnection(collector_id: str, request: FastAPIRequest) -> dict[str, Any]:
        validated_collector_id = _collector_id(collector_id)
        payload = await _body(request)
        expiry = payload.get("expires_in_minutes")
        if expiry not in {15, 30, 60}:
            raise HTTPException(status_code=400, detail="Code expiry must be 15, 30, or 60 minutes")
        try:
            return _client().create_reconnection_code(validated_collector_id, expiry)
        except DashboardClientError as error:
            raise _api_error(error) from error

    @app.delete("/api/v1/collectors/{collector_id}")
    def delete_collector(collector_id: str) -> dict[str, Any]:
        validated_collector_id = _collector_id(collector_id)
        try:
            return _client().delete_collector(validated_collector_id)
        except DashboardClientError as error:
            raise _api_error(error) from error

    if _DIST_DIRECTORY.is_dir():
        assets = _DIST_DIRECTORY / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="react-assets")

        @app.get("/{requested_path:path}", include_in_schema=False)
        def react_application(requested_path: str) -> FileResponse:
            return FileResponse(_DIST_DIRECTORY / "index.html")
    else:
        @app.get("/", include_in_schema=False)
        def assets_missing() -> None:
            raise HTTPException(status_code=503, detail="React dashboard assets are unavailable. Build control_plane/dashboard_react first.")
    return app


app = create_react_dashboard_app()

if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8503)
