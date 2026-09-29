"""Minimal authenticated API client for the control-plane Streamlit dashboard."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class DashboardClientError(RuntimeError):
    """Safe error for an unavailable or unauthorized control-plane API."""


class ControlPlaneDashboardClient:
    def __init__(self, base_url: str, dashboard_key: str, admin_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._dashboard_key = dashboard_key
        self._admin_key = admin_key

    def collectors(self) -> list[dict[str, Any]]:
        response = self._get("/api/v1/dashboard/collectors")
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid collector response")
        return response

    def inventory(self, collector_id: str) -> list[dict[str, Any]]:
        response = self._get(f"/api/v1/dashboard/collectors/{collector_id}/inventory")
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid inventory response")
        return response

    def connections(
        self, collector_id: str, observed_after: str, observed_before: str
    ) -> list[dict[str, Any]]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/connections",
                {"observed_after": observed_after, "observed_before": observed_before},
            )
        )
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid connection response")
        return response

    def wave_connections(
        self,
        collector_id: str,
        vm_uuids: list[str],
        observed_after: str,
        observed_before: str,
        page: int,
        page_size: int = 100,
    ) -> dict[str, Any]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/wave-connections",
                {
                    "vm_uuid": vm_uuids,
                    "observed_after": observed_after,
                    "observed_before": observed_before,
                    "page": page,
                    "page_size": page_size,
                },
            )
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("items"), list)
            or not isinstance(response.get("total"), int)
        ):
            raise DashboardClientError("Control plane returned an invalid wave connection response")
        return response

    def detailed_connections(
        self, collector_id: str, observed_after: str, observed_before: str
    ) -> list[dict[str, Any]]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/detailed-connections",
                {"observed_after": observed_after, "observed_before": observed_before},
            )
        )
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid detailed report response")
        return response

    def waves(
        self, collector_id: str, observed_after: str, observed_before: str
    ) -> list[dict[str, Any]]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/waves",
                {"observed_after": observed_after, "observed_before": observed_before},
            )
        )
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid wave response")
        return response

    def wave_summary(
        self, collector_id: str, observed_after: str, observed_before: str
    ) -> dict[str, Any]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/wave-summary",
                {"observed_after": observed_after, "observed_before": observed_before},
            )
        )
        if not isinstance(response, dict):
            raise DashboardClientError("Control plane returned an invalid wave summary response")
        return response

    def create_enrollment(self, tenant_id: str, expires_in_minutes: int) -> dict[str, Any]:
        response = self._post(
            "/api/v1/admin/enrollments",
            {"tenant_id": tenant_id, "expires_in_minutes": expires_in_minutes},
            self._admin_key,
        )
        if not isinstance(response, dict) or not isinstance(response.get("enrollment_code"), str):
            raise DashboardClientError("Control plane returned an invalid enrollment response")
        return response

    def create_reconnection_code(
        self, collector_id: str, expires_in_minutes: int
    ) -> dict[str, Any]:
        response = self._post(
            f"/api/v1/admin/collectors/{collector_id}/reconnection-codes",
            {"expires_in_minutes": expires_in_minutes},
            self._admin_key,
        )
        if not isinstance(response, dict) or not isinstance(response.get("reconnection_code"), str):
            raise DashboardClientError("Control plane returned an invalid reconnection response")
        return response

    def delete_collector(self, collector_id: str) -> dict[str, Any]:
        response = self._delete(
            f"/api/v1/admin/collectors/{quote(collector_id, safe='')}", self._admin_key
        )
        if not isinstance(response, dict) or response.get("status") != "deleted":
            raise DashboardClientError(
                "Control plane returned an invalid collector deletion response"
            )
        return response

    def _get(self, path: str) -> Any:
        request = Request(
            f"{self._base_url}{path}",
            headers={"X-Control-Plane-Key": self._dashboard_key},
            method="GET",
        )
        return self._read_response(request)

    def _post(self, path: str, payload: dict[str, Any], key: str) -> Any:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Control-Plane-Key": key},
            method="POST",
        )
        return self._read_response(request)

    def _delete(self, path: str, key: str) -> Any:
        request = Request(
            f"{self._base_url}{path}",
            headers={"X-Control-Plane-Key": key},
            method="DELETE",
        )
        return self._read_response(request)

    @staticmethod
    def _read_response(request: Request) -> Any:
        try:
            with urlopen(request, timeout=15) as response:  # noqa: S310 - operator config
                return json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DashboardClientError("Control-plane dashboard request failed") from error

    @staticmethod
    def _with_query(path: str, values: dict[str, Any]) -> str:
        return f"{path}?{urlencode(values, doseq=True)}"
