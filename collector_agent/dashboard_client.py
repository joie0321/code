"""Loopback client used by the local collector-appliance dashboard."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AgentDashboardClientError(RuntimeError):
    """Safe error returned by the local collector agent."""


class AgentDashboardClient:
    """Calls the local-only agent setup API; no customer secrets leave this host."""

    def __init__(self, base_url: str = "http://127.0.0.1:8443") -> None:
        self._base_url = base_url.rstrip("/")

    def status(self) -> dict[str, Any]:
        result = self._request("/api/v1/status", method="GET")
        if not isinstance(result, dict):
            raise AgentDashboardClientError("Agent returned an invalid status response")
        return result

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("/api/v1/setup/register", payload)

    def reconnect(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("/api/v1/setup/reconnect", payload)

    def configure_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("/api/v1/setup/credentials", payload)

    def sync_inventory(self) -> dict[str, Any]:
        return self._request("/api/v1/collection/inventory/sync", {})

    def ipfix_setup(self) -> dict[str, Any]:
        result = self._request("/api/v1/ipfix/setup", method="GET")
        if not isinstance(result, dict):
            raise AgentDashboardClientError("Agent returned an invalid IPFIX setup response")
        return result

    def start_ipfix(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("/api/v1/ipfix/start", payload)

    def stop_ipfix(self) -> dict[str, Any]:
        return self._request("/api/v1/ipfix/stop", {})

    def _request(
        self, path: str, payload: dict[str, Any] | None = None, method: str = "POST"
    ) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method=method,
        )
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed loopback service
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            raise AgentDashboardClientError("Collector agent request was rejected") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise AgentDashboardClientError("Cannot contact the local collector agent") from error
