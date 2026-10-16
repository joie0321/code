"""Loopback-only host for the separate React collector dashboard.

This module deliberately does not replace or alter ``collector_agent/dashboard.py``.
It hosts a built React application and proxies an explicit allow-list of local
Collector setup actions. It never acts as a general-purpose HTTP proxy.
"""

from __future__ import annotations

import json
from pathlib import Path
from re import fullmatch
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi import Request as FastAPIRequest
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

_COLLECTOR_API_ORIGIN = "http://127.0.0.1:8443"
_STATUS_PATH = "/api/v1/status"
_JSON_POST_PATHS = frozenset(
    {
        "/api/v1/setup/register",
        "/api/v1/setup/reconnect",
        "/api/v1/setup/credentials",
        "/api/v1/setup/azure/credentials",
        "/api/v1/setup/azure/flow-logs",
        "/api/v1/setup/aws/credentials",
        "/api/v1/setup/aws/flow-logs",
        "/api/v1/ipfix/start",
    }
)
_DIST_DIRECTORY = Path(__file__).with_name("dashboard_react") / "dist"
_MAX_REQUEST_BODY_BYTES = 16 * 1024


class CollectorRequestRejected(RuntimeError):
    """A safe validation failure returned by the fixed local Collector API."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code if 400 <= status_code < 500 else 502


def _collector_url(path: str) -> str:
    return f"{_COLLECTOR_API_ORIGIN}{path}"


def _safe_error_detail(error: HTTPError) -> str:
    """Return the small, API-provided validation message when it is safe to show."""

    try:
        payload = json.loads(error.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "Collector agent request was rejected"
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and 0 < len(detail) <= 512:
        return detail
    return "Collector agent request was rejected"


def _collector_request(path: str, method: str, body: bytes | None = None) -> object:
    """Call one fixed loopback Collector API endpoint without logging its body."""

    request = Request(
        _collector_url(path),
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed loopback service
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise CollectorRequestRejected(error.code, _safe_error_detail(error)) from error
    except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Cannot contact the local collector agent") from error


def fetch_agent_status() -> dict[str, object]:
    """Read status from the fixed loopback Collector API.

    The React host is intentionally not a general-purpose proxy. Keeping the
    upstream endpoint fixed avoids turning the local dashboard into an SSRF
    primitive as more React pages are introduced in later phases.
    """

    payload = _collector_request(_STATUS_PATH, "GET")
    if not isinstance(payload, dict):
        raise RuntimeError("Collector agent returned an invalid status response")
    return payload


async def _request_body(request: FastAPIRequest) -> bytes:
    """Limit and validate browser input before forwarding it to the Collector API."""

    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="JSON request content is required")
    body = await request.body()
    if not body or len(body) > _MAX_REQUEST_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body is invalid or too large")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON") from error
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def _collector_id_or_raise(collector_id: str | None) -> str:
    if collector_id is None or fullmatch(r"[A-Za-z0-9-]{36}", collector_id) is None:
        raise HTTPException(status_code=400, detail="Invalid collector ID")
    return collector_id


def create_react_dashboard_app() -> FastAPI:
    """Create a local-only React dashboard host for the collector appliance."""

    app = FastAPI(
        title="Migration Discovery Collector React Dashboard",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/status")
    def agent_status() -> dict[str, object]:
        try:
            return fetch_agent_status()
        except CollectorRequestRejected as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post("/api/v1/setup/register")
    async def register_source(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/setup/register")

    @app.post("/api/v1/setup/reconnect")
    async def reconnect_source(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/setup/reconnect")

    @app.post("/api/v1/setup/credentials")
    async def configure_vmware(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/setup/credentials")

    @app.post("/api/v1/setup/azure/credentials")
    async def configure_azure(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/setup/azure/credentials")

    @app.post("/api/v1/setup/azure/flow-logs")
    async def configure_azure_flow_logs(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/setup/azure/flow-logs")

    @app.post("/api/v1/setup/aws/credentials")
    async def configure_aws(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/setup/aws/credentials")

    @app.post("/api/v1/setup/aws/flow-logs")
    async def configure_aws_flow_logs(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/setup/aws/flow-logs")

    @app.get("/api/v1/ipfix/setup")
    def ipfix_setup(collector_id: str | None = None) -> JSONResponse:
        return _forward_collector_action("/api/v1/ipfix/setup", "GET", collector_id)

    @app.post("/api/v1/ipfix/start")
    async def start_ipfix(request: FastAPIRequest) -> JSONResponse:
        return await _forward_setup_request(request, "/api/v1/ipfix/start")

    @app.post("/api/v1/ipfix/stop")
    def stop_ipfix(collector_id: str | None = None) -> JSONResponse:
        return _forward_collector_action("/api/v1/ipfix/stop", "POST", collector_id)

    @app.post("/api/v1/collection/inventory/sync")
    def sync_inventory(collector_id: str | None = None) -> JSONResponse:
        return _forward_collector_action("/api/v1/collection/inventory/sync", "POST", collector_id)

    @app.post("/api/v1/aws/flow-logs/sync")
    def sync_aws_flow_logs(collector_id: str | None = None) -> JSONResponse:
        return _forward_collector_action("/api/v1/aws/flow-logs/sync", "POST", collector_id)

    @app.post("/api/v1/azure/flow-logs/sync")
    def sync_azure_flow_logs(collector_id: str | None = None) -> JSONResponse:
        return _forward_collector_action("/api/v1/azure/flow-logs/sync", "POST", collector_id)

    @app.delete("/api/v1/setup/sources/{collector_id}")
    def unregister_source(collector_id: str) -> JSONResponse:
        _collector_id_or_raise(collector_id)
        try:
            payload = _collector_request(
                f"/api/v1/setup/sources/{quote(collector_id, safe='')}",
                "DELETE",
            )
        except CollectorRequestRejected as error:
            raise HTTPException(status_code=error.status_code, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=502,
                detail="Collector agent returned an invalid response",
            )
        return JSONResponse(payload)

    if _DIST_DIRECTORY.is_dir():
        assets_directory = _DIST_DIRECTORY / "assets"
        if assets_directory.is_dir():
            app.mount("/assets", StaticFiles(directory=assets_directory), name="react-assets")

        @app.get("/{requested_path:path}", include_in_schema=False)
        def react_application(requested_path: str) -> FileResponse:
            """Return the SPA entry point; Vite owns client-side routing."""

            index_file = _DIST_DIRECTORY / "index.html"
            if not index_file.is_file():
                raise HTTPException(
                    status_code=503,
                    detail="React dashboard assets are unavailable",
                )
            return FileResponse(index_file)

    else:

        @app.get("/", include_in_schema=False)
        def react_assets_missing() -> dict[str, str]:
            raise HTTPException(
                status_code=503,
                detail=(
                    "React dashboard assets are unavailable. "
                    "Build collector_agent/dashboard_react first."
                ),
            )

    return app


async def _forward_setup_request(request: FastAPIRequest, path: str) -> JSONResponse:
    """Forward one explicitly allow-listed setup request to the local Collector API."""

    if path not in _JSON_POST_PATHS:
        raise HTTPException(status_code=404, detail="Unsupported Collector setup route")
    body = await _request_body(request)
    try:
        payload = _collector_request(path, "POST", body)
    except CollectorRequestRejected as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Collector agent returned an invalid response")
    return JSONResponse(payload)


def _forward_collector_action(path: str, method: str, collector_id: str | None) -> JSONResponse:
    """Forward a fixed action for one validated source; no arbitrary URL is accepted."""

    validated_id = _collector_id_or_raise(collector_id)
    try:
        payload = _collector_request(f"{path}?collector_id={quote(validated_id, safe='')}", method)
    except CollectorRequestRejected as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail="Collector agent returned an invalid response")
    return JSONResponse(payload)


app = create_react_dashboard_app()


if __name__ == "__main__":  # pragma: no cover - operational entry point
    import uvicorn

    # Do not expose an unauthenticated local configuration dashboard on the network.
    uvicorn.run(app, host="127.0.0.1", port=8502)
