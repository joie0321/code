"""In-memory customer-side collection-source state and control-plane delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from typing import Any

from pydantic import SecretStr

from collector_agent.aws import AwsCredentials
from collector_agent.aws import fetch_vms as fetch_aws_vms
from collector_agent.aws_flow_logs import (
    AwsFlowLogError,
    AwsFlowLogReadResult,
    AwsFlowLogSettings,
    fetch_flow_log_observations,
)
from collector_agent.azure import AzureCredentials
from collector_agent.azure import fetch_vms as fetch_azure_vms
from collector_agent.client import ControlPlaneClient, ControlPlaneClientError
from collector_agent.config import AgentSettings
from collector_agent.ipfix import IpfixFlow, IpfixListener
from collector_agent.schemas import (
    AwsCredentialsRequest,
    AwsFlowLogSettingsRequest,
    AzureCredentialsRequest,
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
    source_type: str = "vmware"
    vcenter_credentials: VCenterCredentials | None = None
    azure_credentials: AzureCredentials | None = None
    aws_credentials: AwsCredentials | None = None
    aws_flow_log_settings: AwsFlowLogSettings | None = None
    aws_flow_log_object_keys: set[str] = field(default_factory=set)
    aws_flow_log_last_sync_at: datetime | None = None
    aws_flow_log_objects_processed: int = 0
    aws_flow_log_observations_uploaded: int = 0
    aws_flow_log_last_error: str | None = None
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
        azure_inventory_fetcher: Any = fetch_azure_vms,
        aws_inventory_fetcher: Any = fetch_aws_vms,
        aws_flow_log_fetcher: Any = fetch_flow_log_observations,
    ):
        settings.validate_runtime()
        self._settings = settings
        self._client_type = client_type
        self._inventory_fetcher = inventory_fetcher
        self._exporter_fetcher = exporter_fetcher
        self._azure_inventory_fetcher = azure_inventory_fetcher
        self._aws_inventory_fetcher = aws_inventory_fetcher
        self._aws_flow_log_fetcher = aws_flow_log_fetcher
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
            source_type=request.source_type,
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
                    source_type=request.source_type,
                    observation_sequence=self._next_sequence(result, "next_observation_sequence")
                    - 1,
                    inventory_sequence=self._next_sequence(result, "next_inventory_sequence") - 1,
                )
        self._start_heartbeat_thread()
        return {"collector_id": collector_id, "status": "reconnected"}

    def unregister(self, collector_id: str) -> dict[str, str]:
        """Remove one source from this appliance without deleting control-plane history."""

        with self._lock:
            profile = self._source_or_raise(collector_id)
            for address in profile.exporters:
                self._exporter_source_ids.pop(address, None)
            self._sources.pop(collector_id)
            all_exporters = sorted(self._exporter_source_ids)
        self._restart_ipfix_listener(all_exporters)
        return {"collector_id": collector_id, "status": "unregistered"}

    def configure_credentials(self, request: LocalCredentialsRequest) -> None:
        """Keep source credentials in appliance memory; never log or upload them."""

        if self._settings.app_env != "development" and not request.verify_tls:
            raise ValueError("vCenter TLS verification is required outside development")
        with self._lock:
            profile = self._source_or_raise(request.collector_id)
            if profile.source_type != "vmware":
                raise ValueError("Select a VMware source before configuring vCenter credentials")
            profile.vcenter_credentials = VCenterCredentials(
                request.vcenter_host,
                request.vcenter_username,
                request.vcenter_password,
                request.verify_tls,
            )
            profile.guest_password = request.guest_password
            profile.last_error = None

    def configure_azure_credentials(self, request: AzureCredentialsRequest) -> None:
        """Keep Azure application credentials in appliance memory only."""

        with self._lock:
            profile = self._source_or_raise(request.collector_id)
            if profile.source_type != "azure":
                raise ValueError("Select an Azure source before configuring Azure credentials")
            profile.azure_credentials = AzureCredentials(
                tenant_id=request.azure_tenant_id,
                client_id=request.azure_client_id,
                client_secret=request.azure_client_secret,
                subscription_ids=request.subscription_ids,
            )
            profile.last_error = None

    def configure_aws_credentials(self, request: AwsCredentialsRequest) -> None:
        """Keep AWS authentication configuration in appliance memory only."""

        with self._lock:
            profile = self._source_or_raise(request.collector_id)
            if profile.source_type != "aws":
                raise ValueError("Select an AWS source before configuring AWS credentials")
            profile.aws_credentials = AwsCredentials(
                region_name=request.aws_region,
                access_key_id=request.aws_access_key_id,
                secret_access_key=request.aws_secret_access_key,
                session_token=request.aws_session_token,
            )
            profile.last_error = None

    def configure_aws_flow_logs(self, request: AwsFlowLogSettingsRequest) -> None:
        """Configure a read-only, customer-owned S3 VPC Flow Log location."""

        with self._lock:
            profile = self._source_or_raise(request.collector_id)
            if profile.source_type != "aws":
                raise ValueError("Select an AWS source before configuring VPC Flow Logs")
            profile.aws_flow_log_settings = AwsFlowLogSettings(
                bucket_name=request.s3_bucket_name,
                prefix=request.s3_prefix,
            )
            profile.aws_flow_log_object_keys.clear()
            profile.aws_flow_log_last_error = None

    def sync_aws_flow_logs(self, collector_id: str) -> dict[str, int]:
        """Read unseen VPC Flow Log objects and upload inventory-matched observations."""

        with self._lock:
            profile = self._source_or_raise(collector_id)
            if profile.source_type != "aws":
                raise ValueError("VPC Flow Logs are available only for AWS sources")
            if profile.aws_credentials is None:
                raise ValueError("Configure AWS credentials before synchronizing VPC Flow Logs")
            if profile.aws_flow_log_settings is None:
                raise ValueError("Configure the S3 VPC Flow Log location before synchronizing")
            credentials = profile.aws_credentials
            settings = profile.aws_flow_log_settings
            known_object_keys = set(profile.aws_flow_log_object_keys)
            source_vm_uuid_by_ip = dict(profile.source_vm_uuid_by_ip)
        try:
            result: AwsFlowLogReadResult = self._aws_flow_log_fetcher(
                credentials, settings, known_object_keys
            )
        except AwsFlowLogError as error:
            message = str(error)
            with self._lock:
                self._source_or_raise(collector_id).aws_flow_log_last_error = message
            raise ValueError(message) from error
        except Exception as error:
            message = "AWS VPC Flow Log synchronization failed. Check local agent settings."
            with self._lock:
                self._source_or_raise(collector_id).aws_flow_log_last_error = message
            raise ValueError(message) from error
        observations = [
            {
                "source_vm_uuid": source_vm_uuid_by_ip[item.source_ip],
                "source_ip": item.source_ip,
                "destination_ip": item.destination_ip,
                "destination_port": item.destination_port,
                "protocol": item.protocol,
                "collector_type": "aws_vpc_flow_logs",
                "observed_at": item.observed_at.isoformat(),
                "process": "aws-vpc-flow-log",
            }
            for item in result.observations
            if item.source_ip in source_vm_uuid_by_ip
        ]
        accepted = 0
        try:
            for offset in range(0, len(observations), 5_000):
                accepted += self.upload_observations(
                    collector_id, observations[offset : offset + 5_000]
                )["accepted"]
        except ControlPlaneClientError as error:
            # ControlPlaneClient exposes only bounded, safe diagnostic text.
            # Preserve it so the appliance operator can distinguish an expired
            # token from a control-plane schema mismatch or connectivity issue.
            message = str(error)
            with self._lock:
                self._source_or_raise(collector_id).aws_flow_log_last_error = message
            raise
        with self._lock:
            profile = self._source_or_raise(collector_id)
            profile.aws_flow_log_object_keys.update(result.processed_object_keys)
            profile.aws_flow_log_last_sync_at = datetime.now(UTC)
            profile.aws_flow_log_objects_processed += len(result.processed_object_keys)
            profile.aws_flow_log_observations_uploaded += accepted
            profile.aws_flow_log_last_error = None
        return {
            "objects_found": result.objects_found,
            "objects_processed": len(result.processed_object_keys),
            "observations_uploaded": accepted,
        }

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
            source_type = profile.source_type
            vmware_credentials = profile.vcenter_credentials
            azure_credentials = profile.azure_credentials
            aws_credentials = profile.aws_credentials
            identity = profile.identity
        if source_type == "vmware":
            if vmware_credentials is None:
                raise ValueError("Configure vCenter credentials before inventory synchronization")
            inventory_fetcher, credentials, source_label = (
                self._inventory_fetcher,
                vmware_credentials,
                "vCenter",
            )
        elif source_type == "azure":
            if azure_credentials is None:
                raise ValueError("Configure Azure credentials before inventory synchronization")
            inventory_fetcher, credentials, source_label = (
                self._azure_inventory_fetcher,
                azure_credentials,
                "Azure",
            )
        elif source_type == "aws":
            if aws_credentials is None:
                raise ValueError("Configure AWS credentials before inventory synchronization")
            inventory_fetcher, credentials, source_label = (
                self._aws_inventory_fetcher,
                aws_credentials,
                "AWS",
            )
        else:  # Defensive: schema validation prevents this for new registrations.
            raise ValueError("This source type does not support inventory synchronization")
        try:
            records: list[VmRecord] = inventory_fetcher(credentials)
        except Exception as error:
            self._set_error(
                collector_id,
                f"{source_label} inventory synchronization failed. Check local agent settings.",
            )
            raise ValueError(
                f"{source_label} inventory synchronization failed. Check local agent settings."
            ) from error
        candidates: list[ExporterCandidate] = []
        if source_type == "vmware":
            try:
                candidates = self._exporter_fetcher(credentials)
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
            if profile.source_type != "vmware":
                raise ValueError("IPFIX is available only for VMware sources")
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
            if profile.source_type != "vmware":
                raise ValueError("IPFIX is available only for VMware sources")
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
            if profile.source_type != "vmware":
                raise ValueError("IPFIX is available only for VMware sources")
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
        if profile.source_type == "vmware":
            ipfix_status, ipfix_detail = self._ipfix_health(profile, source_listener)
        else:
            ipfix_status, ipfix_detail = (
                "Not applicable",
                f"IPFIX is not used by {profile.source_type.title()} sources.",
            )
        aws_flow_log_status, aws_flow_log_detail = self._aws_flow_log_health(profile)
        return {
            "collector_id": profile.identity.collector_id,
            "display_name": profile.display_name,
            "source_type": profile.source_type,
            "vcenter_configured": profile.vcenter_credentials is not None,
            "azure_configured": profile.azure_credentials is not None,
            "aws_configured": profile.aws_credentials is not None,
            "aws_flow_logs_configured": profile.aws_flow_log_settings is not None,
            "aws_flow_log_status": aws_flow_log_status,
            "aws_flow_log_detail": aws_flow_log_detail,
            "aws_flow_log_last_sync_at": self._timestamp(profile.aws_flow_log_last_sync_at),
            "aws_flow_log_objects_processed": profile.aws_flow_log_objects_processed,
            "aws_flow_log_observations_uploaded": profile.aws_flow_log_observations_uploaded,
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
    def _aws_flow_log_health(profile: _SourceProfile) -> tuple[str, str]:
        if profile.source_type != "aws":
            return "Not applicable", "VPC Flow Logs are used only by AWS sources."
        if profile.aws_flow_log_settings is None:
            return "Not configured", "No S3 VPC Flow Log location is configured."
        if profile.aws_flow_log_last_error:
            return "Error", profile.aws_flow_log_last_error
        if profile.aws_flow_log_last_sync_at is None:
            return "Waiting", "No VPC Flow Log synchronization has completed yet."
        if profile.aws_flow_log_observations_uploaded == 0:
            return "Waiting for traffic", "No accepted inventory-matched VPC flows were found yet."
        return "OK", "AWS VPC Flow Log observations are uploading for this source."

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
                    should_sync_aws_flow_logs = (
                        profile.source_type == "aws"
                        and profile.aws_flow_log_settings is not None
                        and (
                            profile.aws_flow_log_last_sync_at is None
                            or datetime.now(UTC) - profile.aws_flow_log_last_sync_at
                            >= timedelta(minutes=5)
                        )
                    )
                    if should_sync_aws_flow_logs:
                        try:
                            self.sync_aws_flow_logs(profile.identity.collector_id)
                        except (ControlPlaneClientError, ValueError):
                            pass
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
