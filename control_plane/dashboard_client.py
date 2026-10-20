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

    def inventory(
        self,
        collector_id: str,
        include_historical: bool = False,
        historical_as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        query: dict[str, Any] = {"include_historical": include_historical}
        if historical_as_of is not None:
            query["historical_as_of"] = historical_as_of
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/inventory",
                query,
            )
        )
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid inventory response")
        return response

    def connections(
        self,
        collector_id: str,
        observed_after: str | None = None,
        observed_before: str | None = None,
        traffic_batch_id: str | None = None,
        traffic_received_after: str | None = None,
    ) -> list[dict[str, Any]]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/connections",
                {
                    "observed_after": observed_after,
                    "observed_before": observed_before,
                    "traffic_batch_id": traffic_batch_id,
                    "traffic_received_after": traffic_received_after,
                },
            )
        )
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid connection response")
        return response

    def wave_connections(
        self,
        collector_id: str,
        vm_uuids: list[str],
        observed_after: str | None,
        observed_before: str | None,
        page: int,
        page_size: int = 100,
        deduplicate: bool = False,
        exclude_dynamic_private_ports: bool = False,
        traffic_batch_id: str | None = None,
        traffic_received_after: str | None = None,
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
                    "deduplicate": deduplicate,
                    "exclude_dynamic_private_ports": exclude_dynamic_private_ports,
                    "traffic_batch_id": traffic_batch_id,
                    "traffic_received_after": traffic_received_after,
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

    def dependencies(
        self, collector_id: str, observed_after: str, observed_before: str
    ) -> dict[str, Any]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/dependencies",
                {"observed_after": observed_after, "observed_before": observed_before},
            )
        )
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise DashboardClientError("Control plane returned an invalid dependency response")
        return response

    def save_dependency_decision(
        self, collector_id: str, payload: dict[str, str]
    ) -> dict[str, Any]:
        response = self._put(
            f"/api/v1/dashboard/collectors/{collector_id}/dependency-decisions",
            payload,
            self._dashboard_key,
        )
        if not isinstance(response, dict) or response.get("status") != "saved":
            raise DashboardClientError(
                "Control plane returned an invalid dependency decision response"
            )
        return response

    def save_dependency_decisions(
        self, collector_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._put(
            f"/api/v1/dashboard/collectors/{collector_id}/dependency-decisions/batch",
            payload,
            self._dashboard_key,
        )
        if not isinstance(response, dict) or response.get("status") != "saved":
            raise DashboardClientError(
                "Control plane returned an invalid dependency decision response"
            )
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
        self,
        collector_id: str,
        observed_after: str | None = None,
        observed_before: str | None = None,
        use_approved_plan: bool = True,
        historical: bool = False,
        traffic_batch_id: str | None = None,
        traffic_received_after: str | None = None,
    ) -> list[dict[str, Any]]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/waves",
                {
                    "observed_after": observed_after,
                    "observed_before": observed_before,
                    "use_approved_plan": use_approved_plan,
                    "historical": historical,
                    "traffic_batch_id": traffic_batch_id,
                    "traffic_received_after": traffic_received_after,
                },
            )
        )
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid wave response")
        return response

    def migration_plans(self, collector_id: str) -> list[dict[str, Any]]:
        response = self._get(f"/api/v1/dashboard/collectors/{collector_id}/migration-plans")
        if not isinstance(response, list):
            raise DashboardClientError("Control plane returned an invalid migration plan response")
        return response

    def plan_drift(self, collector_id: str) -> dict[str, Any]:
        response = self._get(f"/api/v1/dashboard/collectors/{collector_id}/plan-drift")
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            raise DashboardClientError("Control plane returned an invalid plan drift response")
        return response

    def migration_plan(self, collector_id: str, plan_id: str) -> dict[str, Any]:
        response = self._get(
            f"/api/v1/dashboard/collectors/{collector_id}/migration-plans/{quote(plan_id, safe='')}"
        )
        if not isinstance(response, dict) or not isinstance(response.get("assignments"), list):
            raise DashboardClientError("Control plane returned an invalid migration plan detail")
        return response

    def create_migration_plan(
        self, collector_id: str, payload: dict[str, str], observed_after: str, observed_before: str
    ) -> dict[str, Any]:
        response = self._post(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/migration-plans",
                {"observed_after": observed_after, "observed_before": observed_before},
            ),
            payload,
            self._dashboard_key,
        )
        if not isinstance(response, dict) or not isinstance(response.get("plan_id"), str):
            raise DashboardClientError("Control plane returned an invalid new migration plan")
        return response

    def clone_migration_plan(
        self, collector_id: str, plan_id: str, payload: dict[str, str]
    ) -> dict[str, Any]:
        response = self._post(
            f"/api/v1/dashboard/collectors/{collector_id}/migration-plans/{quote(plan_id, safe='')}/versions",
            payload,
            self._dashboard_key,
        )
        if not isinstance(response, dict) or not isinstance(response.get("plan_id"), str):
            raise DashboardClientError("Control plane returned an invalid migration plan version")
        return response

    def update_migration_plan_assignment(
        self, collector_id: str, plan_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._put(
            f"/api/v1/dashboard/collectors/{collector_id}/migration-plans/{quote(plan_id, safe='')}/assignments",
            payload,
            self._dashboard_key,
        )
        if not isinstance(response, dict) or response.get("status") != "saved":
            raise DashboardClientError("Control plane returned an invalid migration plan update")
        return response

    def update_migration_plan_assignments(
        self, collector_id: str, plan_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._put(
            f"/api/v1/dashboard/collectors/{collector_id}/migration-plans/{quote(plan_id, safe='')}/bulk-assignments",
            payload,
            self._dashboard_key,
        )
        if not isinstance(response, dict) or response.get("status") != "saved":
            raise DashboardClientError(
                "Control plane returned an invalid migration plan bulk update"
            )
        return response

    def approve_migration_plan(
        self, collector_id: str, plan_id: str, payload: dict[str, str]
    ) -> dict[str, Any]:
        response = self._post(
            f"/api/v1/dashboard/collectors/{collector_id}/migration-plans/{quote(plan_id, safe='')}/approve",
            payload,
            self._dashboard_key,
        )
        if not isinstance(response, dict) or response.get("status") != "approved":
            raise DashboardClientError("Control plane returned an invalid migration plan approval")
        return response

    def wave_summary(
        self,
        collector_id: str,
        observed_after: str | None = None,
        observed_before: str | None = None,
        use_approved_plan: bool = True,
        historical: bool = False,
        traffic_batch_id: str | None = None,
        traffic_received_after: str | None = None,
    ) -> dict[str, Any]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/wave-summary",
                {
                    "observed_after": observed_after,
                    "observed_before": observed_before,
                    "use_approved_plan": use_approved_plan,
                    "historical": historical,
                    "traffic_batch_id": traffic_batch_id,
                    "traffic_received_after": traffic_received_after,
                },
            )
        )
        if not isinstance(response, dict):
            raise DashboardClientError("Control plane returned an invalid wave summary response")
        return response

    def current_environment(
        self,
        collector_id: str,
        observed_after: str | None = None,
        observed_before: str | None = None,
        use_approved_plan: bool = True,
        historical: bool = False,
        traffic_batch_id: str | None = None,
        traffic_received_after: str | None = None,
    ) -> dict[str, Any]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/current-environment",
                {
                    "observed_after": observed_after,
                    "observed_before": observed_before,
                    "use_approved_plan": use_approved_plan,
                    "historical": historical,
                    "traffic_batch_id": traffic_batch_id,
                    "traffic_received_after": traffic_received_after,
                },
            )
        )
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("connections"), list)
            or not isinstance(response.get("waves"), list)
            or not isinstance(response.get("summary"), dict)
        ):
            raise DashboardClientError(
                "Control plane returned an invalid current environment response"
            )
        return response

    def wave_grouping_port_allowlist(self, collector_id: str) -> dict[str, Any]:
        response = self._get(
            f"/api/v1/dashboard/collectors/{collector_id}/wave-grouping-port-allowlist"
        )
        if not isinstance(response, dict) or not isinstance(response.get("ports"), list):
            raise DashboardClientError("Control plane returned an invalid wave-grouping policy")
        return response

    def update_wave_grouping_port_allowlist(
        self, collector_id: str, ports: list[dict[str, Any]]
    ) -> dict[str, Any]:
        response = self._put(
            f"/api/v1/dashboard/collectors/{collector_id}/wave-grouping-port-allowlist",
            {"ports": ports},
            self._dashboard_key,
        )
        if not isinstance(response, dict) or not isinstance(response.get("ports"), list):
            raise DashboardClientError("Control plane returned an invalid wave-grouping policy")
        return response

    def wave_readiness(
        self,
        collector_id: str,
        observed_after: str,
        observed_before: str,
        use_approved_plan: bool = True,
    ) -> dict[str, Any]:
        response = self._get(
            self._with_query(
                f"/api/v1/dashboard/collectors/{collector_id}/wave-readiness",
                {
                    "observed_after": observed_after,
                    "observed_before": observed_before,
                    "use_approved_plan": use_approved_plan,
                },
            )
        )
        if not isinstance(response, dict) or not isinstance(response.get("waves"), list):
            raise DashboardClientError("Control plane returned an invalid wave readiness response")
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

    def _put(self, path: str, payload: dict[str, Any], key: str) -> Any:
        request = Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Control-Plane-Key": key},
            method="PUT",
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
        query_values = {key: value for key, value in values.items() if value is not None}
        return f"{path}?{urlencode(query_values, doseq=True)}"
