"""Small HTTPS client used by the customer-side collector agent."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ControlPlaneClientError(RuntimeError):
    """Safe, non-secret error for failed control-plane interactions."""


class ControlPlaneClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def register(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("/api/v1/agents/register", payload)

    def reconnect(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("/api/v1/agents/reconnect", payload)

    def heartbeat(
        self, collector_id: str, agent_token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/agents/heartbeat",
            payload,
            {"X-Collector-Id": collector_id, "X-Agent-Token": agent_token},
        )

    def upload_observations(
        self, collector_id: str, agent_token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/agents/observations",
            payload,
            {"X-Collector-Id": collector_id, "X-Agent-Token": agent_token},
        )

    def upload_inventory(
        self, collector_id: str, agent_token: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "/api/v1/agents/inventory",
            payload,
            {"X-Collector-Id": collector_id, "X-Agent-Token": agent_token},
        )

    def _request(
        self, path: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:  # noqa: S310 - URL is operator config.
                parsed = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ControlPlaneClientError("Control-plane request failed") from error
        if not isinstance(parsed, dict):
            raise ControlPlaneClientError("Control plane returned an invalid response")
        return parsed
