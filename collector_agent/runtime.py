"""In-memory customer-side collector state and control-plane delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from typing import Any

from pydantic import SecretStr

from collector_agent.client import ControlPlaneClient, ControlPlaneClientError
from collector_agent.config import AgentSettings
from collector_agent.ipfix import IpfixFlow, IpfixListener
from collector_agent.schemas import (
    IpfixStartRequest,
    LocalCredentialsRequest,
    ReconnectRequest,
    RegistrationRequest,
)
from collector_agent.vcenter import (
    ExporterCandidate,
    VCenterCredentials,
    VmRecord,
    fetch_exporter_candidates,
    fetch_vms,
)


@dataclass(frozen=True)
class _AgentIdentity:
    control_plane_url: str
    collector_id: str
    agent_token: str


class AgentRuntime:
    """Owns ephemeral credentials and sends only normalized non-secret telemetry."""

    def __init__(
        self,
        settings: AgentSettings,
        client_type: type[ControlPlaneClient] = ControlPlaneClient,
        inventory_fetcher: Any = fetch_vms,
        exporter_fetcher: Any = fetch_exporter_candidates,
    ):
        settings.validate_runtime()
        self._settings = settings
        self._client_type = client_type
        self._inventory_fetcher = inventory_fetcher
        self._exporter_fetcher = exporter_fetcher
        self._lock = Lock()
        self._stop = Event()
        self._identity: _AgentIdentity | None = None
        self._vcenter_credentials: VCenterCredentials | None = None
        self._guest_password: SecretStr | None = None
        self._has_vcenter_configuration = False
        self._sequence = 0
        self._inventory_sequence = 0
        self._last_inventory_vm_count = 0
        self._last_inventory_sync_at: datetime | None = None
        self._last_error: str | None = None
        self._last_heartbeat_at: datetime | None = None
        self._heartbeat_thread: Thread | None = None
        self._source_vm_uuid_by_ip: dict[str, str] = {}
        self._exporter_candidates: list[ExporterCandidate] = []
        self._ipfix_listener = IpfixListener(self._upload_ipfix_flows)

    def register(self, request: RegistrationRequest) -> dict[str, str]:
        if self._settings.app_env != "development" and not request.control_plane_url.startswith(
            "https://"
        ):
            raise ValueError("The control-plane URL must use HTTPS outside development")
        result = self._client_type(request.control_plane_url).register(
            {
                "tenant_id": request.tenant_id,
                "enrollment_code": request.enrollment_code.get_secret_value(),
                "display_name": request.display_name,
                "software_version": "0.1.0",
            }
        )
        collector_id = result.get("collector_id")
        agent_token = result.get("agent_token")
        if not isinstance(collector_id, str) or not isinstance(agent_token, str):
            raise ControlPlaneClientError("Control plane returned an invalid registration response")
        with self._lock:
            self._identity = _AgentIdentity(request.control_plane_url, collector_id, agent_token)
            self._sequence = int(result.get("next_observation_sequence", 1)) - 1
            self._inventory_sequence = int(result.get("next_inventory_sequence", 1)) - 1
            self._last_error = None
        self._start_heartbeat_thread()
        return {"collector_id": collector_id, "status": "registered"}

    def reconnect(self, request: ReconnectRequest) -> dict[str, str]:
        """Rotate the token for an existing collector after a verified recovery action."""

        if self._settings.app_env != "development" and not request.control_plane_url.startswith(
            "https://"
        ):
            raise ValueError("The control-plane URL must use HTTPS outside development")
        result = self._client_type(request.control_plane_url).reconnect(
            {
                "collector_id": request.collector_id,
                "reconnection_code": request.reconnection_code.get_secret_value(),
            }
        )
        collector_id = result.get("collector_id")
        agent_token = result.get("agent_token")
        if not isinstance(collector_id, str) or collector_id != request.collector_id:
            raise ControlPlaneClientError("Control plane returned an invalid reconnection response")
        if not isinstance(agent_token, str):
            raise ControlPlaneClientError("Control plane returned an invalid reconnection response")
        next_observation_sequence = result.get("next_observation_sequence", 1)
        next_inventory_sequence = result.get("next_inventory_sequence", 1)
        if not isinstance(next_observation_sequence, int) or not isinstance(
            next_inventory_sequence, int
        ):
            raise ControlPlaneClientError("Control plane returned an invalid reconnection response")
        with self._lock:
            self._identity = _AgentIdentity(request.control_plane_url, collector_id, agent_token)
            self._sequence = next_observation_sequence - 1
            self._inventory_sequence = next_inventory_sequence - 1
            self._last_error = None
        self._start_heartbeat_thread()
        return {"collector_id": collector_id, "status": "reconnected"}

    def configure_credentials(self, request: LocalCredentialsRequest) -> None:
        """Keep customer credentials in memory; do not log or upload them."""

        if self._settings.app_env != "development" and not request.verify_tls:
            raise ValueError("vCenter TLS verification is required outside development")
        with self._lock:
            self._vcenter_credentials = VCenterCredentials(
                request.vcenter_host,
                request.vcenter_username,
                request.vcenter_password,
                request.verify_tls,
            )
            self._guest_password = request.guest_password
            self._has_vcenter_configuration = True

    def upload_observations(self, observations: list[dict[str, Any]]) -> dict[str, Any]:
        identity = self._identity_or_raise()
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        result = self._client_type(identity.control_plane_url).upload_observations(
            identity.collector_id,
            identity.agent_token,
            {"sequence": sequence, "observations": observations},
        )
        return {"accepted": int(result.get("accepted", 0)), "sequence": sequence}

    def sync_inventory(self) -> dict[str, Any]:
        identity = self._identity_or_raise()
        with self._lock:
            credentials = self._vcenter_credentials
        if credentials is None:
            raise ValueError("Configure vCenter credentials before inventory synchronization")
        try:
            records: list[VmRecord] = self._inventory_fetcher(credentials)
        except Exception as error:
            with self._lock:
                self._last_error = (
                    "vCenter inventory synchronization failed. Check local agent settings."
                )
            raise ValueError(
                "vCenter inventory synchronization failed. Check local agent settings."
            ) from error
        try:
            candidates: list[ExporterCandidate] = self._exporter_fetcher(credentials)
        except Exception:
            # Inventory remains useful even if a vCenter permission or host query
            # prevents default exporter discovery; the UI still permits explicit IPs.
            candidates = []
        payload_vms = [
            {
                "vm_uuid": record.vm_uuid,
                "name": record.name,
                "hostname": record.hostname,
                "ips": record.ips,
                "cluster": record.cluster,
                "folder": record.folder,
                "os_name": record.os_name,
                "power_state": record.power_state,
            }
            for record in records
        ]
        with self._lock:
            self._inventory_sequence += 1
            sequence = self._inventory_sequence
        try:
            result = self._client_type(identity.control_plane_url).upload_inventory(
                identity.collector_id,
                identity.agent_token,
                {"sequence": sequence, "vms": payload_vms},
            )
        except ControlPlaneClientError:
            with self._lock:
                self._last_error = "Inventory upload failed. The agent will retry on the next sync."
            raise
        with self._lock:
            self._last_inventory_vm_count = len(records)
            self._last_inventory_sync_at = datetime.now(UTC)
            self._source_vm_uuid_by_ip = {
                address: record.vm_uuid
                for record in records
                if str(record.power_state).lower().endswith("poweredon")
                for address in record.ips
            }
            self._exporter_candidates = candidates
            self._last_error = None
        return {
            "discovered": len(records),
            "accepted": int(result.get("accepted", 0)),
            "sequence": sequence,
        }

    def ipfix_setup(self) -> dict[str, Any]:
        """Expose only non-secret exporter candidates and listener health."""

        with self._lock:
            candidates = [
                {
                    "host_name": candidate.host_name,
                    "cluster": candidate.cluster,
                    "vmk0_ips": candidate.vmk0_ips,
                }
                for candidate in self._exporter_candidates
            ]
        return {"exporter_candidates": candidates, "listener": self._ipfix_listener.status()}

    def start_ipfix(self, request: IpfixStartRequest) -> dict[str, object]:
        identity = self._identity_or_raise()
        with self._lock:
            candidate_ips = {
                address
                for candidate in self._exporter_candidates
                if (candidate.cluster or "Unclustered") in request.selected_clusters
                for address in candidate.vmk0_ips
            }
        exporters = sorted(
            (candidate_ips | set(request.manual_exporters)) - set(request.excluded_exporters)
        )
        if not exporters:
            raise ValueError(
                "Select at least one included exporter or enter an explicit exporter IP"
            )
        # Registration is checked before a network listener is opened.
        if not identity.agent_token:
            raise ValueError("Register the collector before starting IPFIX")
        return self._ipfix_listener.start(exporters)

    def stop_ipfix(self) -> dict[str, object]:
        return self._ipfix_listener.stop()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "registration": "registered" if self._identity else "not_registered",
                "collector_id": self._identity.collector_id if self._identity else None,
                "vcenter_configured": self._has_vcenter_configuration,
                "guest_credentials_configured": self._guest_password is not None,
                "last_heartbeat_at": self._last_heartbeat_at.isoformat()
                if self._last_heartbeat_at
                else None,
                "last_error": self._last_error,
                "last_inventory_sync_at": self._last_inventory_sync_at.isoformat()
                if self._last_inventory_sync_at
                else None,
                "last_inventory_vm_count": self._last_inventory_vm_count,
                "ipfix": self._ipfix_listener.status(),
            }

    def shutdown(self) -> None:
        self._stop.set()
        self._ipfix_listener.stop()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)

    def _upload_ipfix_flows(self, flows: list[IpfixFlow]) -> int:
        """Normalize only flows originating from active, discovered customer VMs."""

        with self._lock:
            source_vm_uuid_by_ip = dict(self._source_vm_uuid_by_ip)
        observations = [
            {
                "source_vm_uuid": source_vm_uuid_by_ip[flow.source_ip],
                "source_ip": flow.source_ip,
                "destination_ip": flow.destination_ip,
                "destination_port": flow.destination_port,
                "protocol": flow.protocol,
                "collector_type": "ipfix",
                "observed_at": flow.observed_at.isoformat(),
                "process": "ipfix",
            }
            for flow in flows
            if flow.source_ip in source_vm_uuid_by_ip
        ]
        return self.upload_observations(observations)["accepted"] if observations else 0

    def _identity_or_raise(self) -> _AgentIdentity:
        with self._lock:
            if self._identity is None:
                raise ValueError("Register the collector before sending telemetry")
            return self._identity

    def _start_heartbeat_thread(self) -> None:
        with self._lock:
            if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
                return
            self._stop.clear()
            self._heartbeat_thread = Thread(
                target=self._heartbeat_loop, daemon=True, name="agent-heartbeat"
            )
            self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._settings.agent_heartbeat_interval_seconds):
            try:
                identity = self._identity_or_raise()
                self._client_type(identity.control_plane_url).heartbeat(
                    identity.collector_id,
                    identity.agent_token,
                    {
                        "software_version": "0.1.0",
                        "inventory_vm_count": self._last_inventory_vm_count,
                    },
                )
                with self._lock:
                    self._last_heartbeat_at = datetime.now(UTC)
                    self._last_error = None
            except (ControlPlaneClientError, ValueError):
                with self._lock:
                    self._last_error = "Control-plane heartbeat failed. The agent will retry."
