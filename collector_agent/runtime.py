"""In-memory customer-side collection-source state and control-plane delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
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
class _SourceIdentity:
    control_plane_url: str
    collector_id: str
    agent_token: str


@dataclass
class _SourceProfile:
    identity: _SourceIdentity
    display_name: str
    vcenter_credentials: VCenterCredentials | None = None
    guest_password: SecretStr | None = None
    observation_sequence: int = 0
    inventory_sequence: int = 0
    inventory_vm_count: int = 0
    last_inventory_sync_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    last_error: str | None = None
    source_vm_uuid_by_ip: dict[str, str] = field(default_factory=dict)
    exporter_candidates: list[ExporterCandidate] = field(default_factory=list)
    exporters: set[str] = field(default_factory=set)
    last_ipfix_error: str | None = None


class AgentRuntime:
    """Own ephemeral credentials for independent VMware collection sources."""

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
        self._sources: dict[str, _SourceProfile] = {}
        self._exporter_source_ids: dict[str, str] = {}
        self._heartbeat_thread: Thread | None = None
        self._ipfix_listener = IpfixListener(self._upload_ipfix_flows)

    def register(self, request: RegistrationRequest) -> dict[str, str]:
        self._validate_control_plane_url(request.control_plane_url)
        result = self._client_type(request.control_plane_url).register(
            {
                "tenant_id": request.tenant_id,
                "enrollment_code": request.enrollment_code.get_secret_value(),
                "display_name": request.display_name,
                "software_version": "0.1.0",
            }
        )
        collector_id, agent_token = self._registration_values(result, "registration")
        profile = _SourceProfile(
            identity=_SourceIdentity(request.control_plane_url, collector_id, agent_token),
            display_name=request.display_name,
            observation_sequence=self._next_sequence(result, "next_observation_sequence") - 1,
            inventory_sequence=self._next_sequence(result, "next_inventory_sequence") - 1,
        )
        with self._lock:
            self._sources[collector_id] = profile
        self._start_heartbeat_thread()
        return {"collector_id": collector_id, "status": "registered"}

    def reconnect(self, request: ReconnectRequest) -> dict[str, str]:
        """Reconnect one source without changing any other local source profile."""

        self._validate_control_plane_url(request.control_plane_url)
        result = self._client_type(request.control_plane_url).reconnect(
            {
                "collector_id": request.collector_id,
                "reconnection_code": request.reconnection_code.get_secret_value(),
            }
        )
        collector_id, agent_token = self._registration_values(result, "reconnection")
        if collector_id != request.collector_id:
            raise ControlPlaneClientError("Control plane returned an invalid reconnection response")
        with self._lock:
            existing = self._sources.get(collector_id)
            if existing:
                existing.identity = _SourceIdentity(
                    request.control_plane_url, collector_id, agent_token
                )
                existing.observation_sequence = (
                    self._next_sequence(result, "next_observation_sequence") - 1
                )
                existing.inventory_sequence = (
                    self._next_sequence(result, "next_inventory_sequence") - 1
                )
                existing.last_error = None
            else:
                self._sources[collector_id] = _SourceProfile(
                    identity=_SourceIdentity(request.control_plane_url, collector_id, agent_token),
                    display_name=request.display_name or collector_id,
                    observation_sequence=self._next_sequence(result, "next_observation_sequence")
                    - 1,
                    inventory_sequence=self._next_sequence(result, "next_inventory_sequence") - 1,
                )
        self._start_heartbeat_thread()
        return {"collector_id": collector_id, "status": "reconnected"}

    def configure_credentials(self, request: LocalCredentialsRequest) -> None:
        """Keep source credentials in appliance memory; never log or upload them."""

        if self._settings.app_env != "development" and not request.verify_tls:
            raise ValueError("vCenter TLS verification is required outside development")
        with self._lock:
            profile = self._source_or_raise(request.collector_id)
            profile.vcenter_credentials = VCenterCredentials(
                request.vcenter_host,
                request.vcenter_username,
                request.vcenter_password,
                request.verify_tls,
            )
            profile.guest_password = request.guest_password
            profile.last_error = None

    def upload_observations(
        self, collector_id: str, observations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        with self._lock:
            profile = self._source_or_raise(collector_id)
            profile.observation_sequence += 1
            sequence = profile.observation_sequence
            identity = profile.identity
        result = self._client_type(identity.control_plane_url).upload_observations(
            identity.collector_id,
            identity.agent_token,
            {"sequence": sequence, "observations": observations},
        )
        return {"accepted": int(result.get("accepted", 0)), "sequence": sequence}

    def sync_inventory(self, collector_id: str) -> dict[str, Any]:
        with self._lock:
            profile = self._source_or_raise(collector_id)
            credentials, identity = profile.vcenter_credentials, profile.identity
        if credentials is None:
            raise ValueError("Configure vCenter credentials before inventory synchronization")
        try:
            records: list[VmRecord] = self._inventory_fetcher(credentials)
        except Exception as error:
            self._set_error(
                collector_id,
                "vCenter inventory synchronization failed. Check local agent settings.",
            )
            raise ValueError(
                "vCenter inventory synchronization failed. Check local agent settings."
            ) from error
        try:
            candidates: list[ExporterCandidate] = self._exporter_fetcher(credentials)
        except Exception:
            candidates = []
        # vCenter can expose the same BIOS UUID through more than one inventory path. The
        # control plane identifies inventory VMs by UUID, so retain one deterministic record.
        payload_vms_by_uuid = {record.vm_uuid: self._vm_payload(record) for record in records}
        payload_vms = list(payload_vms_by_uuid.values())
        sequence = self._next_inventory_sequence(collector_id)
        try:
            result = self._client_type(identity.control_plane_url).upload_inventory(
                identity.collector_id,
                identity.agent_token,
                {"sequence": sequence, "vms": payload_vms},
            )
            if result.get("duplicate") is True:
                # A source can reconnect after a local restart with a stale sequence. Retrying the
                # current snapshot once with a fresh sequence preserves idempotency and avoids a
                # permanently empty control-plane inventory.
                sequence = self._next_inventory_sequence(collector_id)
                result = self._client_type(identity.control_plane_url).upload_inventory(
                    identity.collector_id,
                    identity.agent_token,
                    {"sequence": sequence, "vms": payload_vms},
                )
        except ControlPlaneClientError:
            self._set_error(
                collector_id, "Inventory upload failed. The agent will retry on the next sync."
            )
            raise
        if result.get("duplicate") is True:
            self._set_error(
                collector_id,
                "Inventory upload was rejected as a duplicate after retry. Reconnect the source "
                "and synchronize again.",
            )
            raise ValueError(
                "Inventory upload was rejected as a duplicate after retry. Reconnect the source "
                "and synchronize again."
            )
        with self._lock:
            profile = self._source_or_raise(collector_id)
            profile.inventory_vm_count = len(records)
            profile.last_inventory_sync_at = datetime.now(UTC)
            profile.source_vm_uuid_by_ip = {
                address: record.vm_uuid
                for record in records
                if str(record.power_state).lower().endswith("poweredon")
                for address in record.ips
            }
            profile.exporter_candidates = candidates
            profile.last_error = None
        return {
            "discovered": len(records),
            "accepted": int(result.get("accepted", 0)),
            "sequence": sequence,
        }

    def ipfix_setup(self, collector_id: str) -> dict[str, Any]:
        """Expose non-secret exporter candidates for one selected source."""

        with self._lock:
            profile = self._source_or_raise(collector_id)
            candidates = [
                {
                    "host_name": candidate.host_name,
                    "cluster": candidate.cluster,
                    "vmk0_ips": candidate.vmk0_ips,
                }
                for candidate in profile.exporter_candidates
            ]
            selected_exporters = sorted(profile.exporters)
            last_error = profile.last_error
        listener = self._ipfix_listener.status()
        return {
            "exporter_candidates": candidates,
            "selected_exporters": selected_exporters,
            "listener": self._source_listener_status(listener, selected_exporters),
            "last_error": last_error,
        }

    def start_ipfix(self, collector_id: str, request: IpfixStartRequest) -> dict[str, object]:
        with self._lock:
            profile = self._source_or_raise(collector_id)
            candidate_ips = {
                address
                for candidate in profile.exporter_candidates
                if (candidate.cluster or "Unclustered") in request.selected_clusters
                for address in candidate.vmk0_ips
            }
            exporters = (candidate_ips | set(request.manual_exporters)) - set(
                request.excluded_exporters
            )
            if not exporters:
                raise ValueError(
                    "Select at least one included exporter or enter an explicit exporter IP"
                )
            conflicts = sorted(
                address
                for address in exporters
                if address in self._exporter_source_ids
                and self._exporter_source_ids[address] != collector_id
            )
            if conflicts:
                raise ValueError(
                    "An IPFIX exporter may belong to only one source on this appliance: "
                    + ", ".join(conflicts)
                )
            for address in profile.exporters:
                self._exporter_source_ids.pop(address, None)
            profile.exporters = set(exporters)
            profile.last_ipfix_error = None
            self._exporter_source_ids.update({address: collector_id for address in exporters})
            all_exporters = sorted(self._exporter_source_ids)
        return self._restart_ipfix_listener(all_exporters)

    def stop_ipfix(self, collector_id: str) -> dict[str, object]:
        with self._lock:
            profile = self._source_or_raise(collector_id)
            for address in profile.exporters:
                self._exporter_source_ids.pop(address, None)
            profile.exporters.clear()
            all_exporters = sorted(self._exporter_source_ids)
        return self._restart_ipfix_listener(all_exporters)

    def status(self) -> dict[str, Any]:
        listener = self._ipfix_listener.status()
        with self._lock:
            sources = [self._source_status(profile, listener) for profile in self._sources.values()]
        return {
            "registration": "registered" if sources else "not_registered",
            "sources": sorted(
                sources, key=lambda item: (item["display_name"], item["collector_id"])
            ),
            "ipfix": listener,
            "unassigned_ipfix_datagrams": self._rejected_exporter_rows(listener),
        }

    def shutdown(self) -> None:
        self._stop.set()
        self._ipfix_listener.stop()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2)

    def _upload_ipfix_flows(self, exporter_ip: str, flows: list[IpfixFlow]) -> int:
        """Route each allowlisted exporter only to its assigned source profile."""

        with self._lock:
            collector_id = self._exporter_source_ids.get(exporter_ip)
            if collector_id is None:
                return 0
            source_vm_uuid_by_ip = dict(self._source_or_raise(collector_id).source_vm_uuid_by_ip)
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
        if not observations:
            return 0
        try:
            accepted = self.upload_observations(collector_id, observations)["accepted"]
        except (ControlPlaneClientError, ValueError):
            with self._lock:
                self._source_or_raise(
                    collector_id
                ).last_ipfix_error = "Could not upload IPFIX observations to the control plane."
            raise
        with self._lock:
            self._source_or_raise(collector_id).last_ipfix_error = None
        return accepted

    def _restart_ipfix_listener(self, exporters: list[str]) -> dict[str, object]:
        was_running = self._ipfix_listener.status().get("status") == "running"
        if was_running:
            self._ipfix_listener.stop()
        if not exporters:
            return self._ipfix_listener.status()
        return self._ipfix_listener.start(exporters)

    @staticmethod
    def _source_listener_status(
        listener: dict[str, object], selected_exporters: list[str]
    ) -> dict[str, object]:
        """Reduce shared listener telemetry to the selected collection source only."""

        raw_exporter_stats = listener.get("exporter_stats", {})
        exporter_stats = raw_exporter_stats if isinstance(raw_exporter_stats, dict) else {}
        selected_stats = [
            item
            for exporter in selected_exporters
            if isinstance(item := exporter_stats.get(exporter), dict)
        ]
        latest_received_at = max(
            (
                str(item["last_received_at"])
                for item in selected_stats
                if item.get("last_received_at")
            ),
            default=None,
        )
        return {
            "status": "running"
            if listener.get("status") == "running" and selected_exporters
            else "stopped",
            "bind_host": listener.get("bind_host"),
            "port": listener.get("port"),
            "selected_exporters": selected_exporters,
            "datagrams_received": sum(
                int(item.get("datagrams_received", 0)) for item in selected_stats
            ),
            "flows_received": sum(int(item.get("flows_received", 0)) for item in selected_stats),
            "observations_stored": sum(
                int(item.get("observations_stored", 0)) for item in selected_stats
            ),
            "last_received_at": latest_received_at,
        }

    def _source_status(
        self, profile: _SourceProfile, listener: dict[str, object]
    ) -> dict[str, object]:
        source_listener = self._source_listener_status(listener, sorted(profile.exporters))
        ipfix_status, ipfix_detail = self._ipfix_health(profile, source_listener)
        return {
            "collector_id": profile.identity.collector_id,
            "display_name": profile.display_name,
            "vcenter_configured": profile.vcenter_credentials is not None,
            "guest_credentials_configured": profile.guest_password is not None,
            "last_heartbeat_at": self._timestamp(profile.last_heartbeat_at),
            "last_error": profile.last_error,
            "last_inventory_sync_at": self._timestamp(profile.last_inventory_sync_at),
            "last_inventory_vm_count": profile.inventory_vm_count,
            "ipfix_exporter_count": len(profile.exporters),
            "ipfix_status": ipfix_status,
            "ipfix_detail": ipfix_detail,
        }

    @staticmethod
    def _ipfix_health(profile: _SourceProfile, listener: dict[str, object]) -> tuple[str, str]:
        if not profile.exporters:
            return "Not configured", "No ESXi exporters are assigned to this source."
        if listener["status"] != "running":
            return "Stopped", "The shared UDP 4739 listener is not running."
        if profile.last_ipfix_error:
            return "Upload error", profile.last_ipfix_error
        if int(listener["datagrams_received"]) == 0:
            return "Waiting for traffic", "No datagrams have arrived from this source's exporters."
        if int(listener["flows_received"]) == 0:
            return "No flows", "Datagrams arrived, but no supported IPFIX flow records were found."
        if int(listener["observations_stored"]) == 0:
            return (
                "No inventory match",
                "Flows arrived but did not match a powered-on inventory VM.",
            )
        return "OK", "IPFIX flow observations are uploading for this source."

    @staticmethod
    def _rejected_exporter_rows(listener: dict[str, object]) -> list[dict[str, object]]:
        rejected_exporters = listener.get("rejected_exporters", {})
        if not isinstance(rejected_exporters, dict):
            return []
        return [
            {"exporter_ip": exporter_ip, **stats}
            for exporter_ip, stats in sorted(
                rejected_exporters.items(),
                key=lambda item: str(item[1].get("last_rejected_at", "")),
                reverse=True,
            )
            if isinstance(stats, dict)
        ]

    def _source_or_raise(self, collector_id: str) -> _SourceProfile:
        profile = self._sources.get(collector_id)
        if profile is None:
            raise ValueError("Select a registered collection source")
        return profile

    def _set_error(self, collector_id: str, message: str) -> None:
        with self._lock:
            self._source_or_raise(collector_id).last_error = message

    def _next_inventory_sequence(self, collector_id: str) -> int:
        with self._lock:
            profile = self._source_or_raise(collector_id)
            profile.inventory_sequence += 1
            return profile.inventory_sequence

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
            with self._lock:
                profiles = list(self._sources.values())
            for profile in profiles:
                try:
                    self._client_type(profile.identity.control_plane_url).heartbeat(
                        profile.identity.collector_id,
                        profile.identity.agent_token,
                        {
                            "software_version": "0.1.0",
                            "inventory_vm_count": profile.inventory_vm_count,
                        },
                    )
                    with self._lock:
                        current = self._sources.get(profile.identity.collector_id)
                        if current:
                            current.last_heartbeat_at = datetime.now(UTC)
                            current.last_error = None
                except ControlPlaneClientError:
                    self._set_error(
                        profile.identity.collector_id,
                        "Control-plane heartbeat failed. The agent will retry.",
                    )

    def _validate_control_plane_url(self, control_plane_url: str) -> None:
        if self._settings.app_env != "development" and not control_plane_url.startswith("https://"):
            raise ValueError("The control-plane URL must use HTTPS outside development")

    @staticmethod
    def _registration_values(result: dict[str, Any], action: str) -> tuple[str, str]:
        collector_id, agent_token = result.get("collector_id"), result.get("agent_token")
        if not isinstance(collector_id, str) or not isinstance(agent_token, str):
            raise ControlPlaneClientError(f"Control plane returned an invalid {action} response")
        return collector_id, agent_token

    @staticmethod
    def _next_sequence(result: dict[str, Any], name: str) -> int:
        value = result.get(name, 1)
        if not isinstance(value, int) or value < 1:
            raise ControlPlaneClientError("Control plane returned an invalid sequence response")
        return value

    @staticmethod
    def _timestamp(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    @staticmethod
    def _vm_payload(record: VmRecord) -> dict[str, object]:
        return {
            "vm_uuid": record.vm_uuid,
            "name": record.name,
            "hostname": record.hostname,
            "ips": record.ips,
            "cluster": record.cluster,
            "folder": record.folder,
            "os_name": record.os_name,
            "power_state": record.power_state,
        }
