"""Bounded reader for customer-owned Azure Virtual Network Flow Log blobs."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from itertools import islice

from azure.core.exceptions import AzureError

from collector_agent.azure import AzureCredentials

_MAX_OBJECT_BYTES = 64 * 1024 * 1024
_MAX_OBJECTS_PER_SYNC = 250


@dataclass(frozen=True)
class AzureFlowLogSettings:
    """Non-secret Azure Blob location of Virtual Network Flow Logs."""

    storage_account_name: str
    container_name: str
    prefix: str


@dataclass(frozen=True)
class AzureFlowObservation:
    source_ip: str
    destination_ip: str
    destination_port: int
    protocol: str
    observed_at: datetime


@dataclass(frozen=True)
class AzureFlowLogReadResult:
    observations: list[AzureFlowObservation]
    processed_object_versions: set[str]
    objects_found: int


class AzureFlowLogError(ValueError):
    """Sanitized Azure Blob retrieval failure safe for the local operator UI."""


def _object_version(name: str, etag: object) -> str:
    return f"{name}\x00{etag if isinstance(etag, str) else ''}"


def _timestamp(value: str) -> datetime | None:
    try:
        epoch = int(value)
        if epoch > 100_000_000_000:
            epoch /= 1000
        return datetime.fromtimestamp(epoch, UTC)
    except (OSError, ValueError, OverflowError):
        return None


def parse_flow_log_document(document: object) -> list[AzureFlowObservation]:
    """Parse Azure VNet Flow Log v4 tuples, rejecting denied or invalid traffic."""

    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        return []
    observations: list[AzureFlowObservation] = []
    for record in document["records"]:
        flow_records = record.get("flowRecords") if isinstance(record, dict) else None
        flows = flow_records.get("flows") if isinstance(flow_records, dict) else None
        if not isinstance(flows, list):
            continue
        for flow in flows:
            groups = flow.get("flowGroups") if isinstance(flow, dict) else None
            if not isinstance(groups, list):
                continue
            for group in groups:
                tuples = group.get("flowTuples") if isinstance(group, dict) else None
                if not isinstance(tuples, list):
                    continue
                observations.extend(_parse_tuples(item for item in tuples if isinstance(item, str)))
    return observations


def _parse_tuples(tuples: Iterable[str]) -> list[AzureFlowObservation]:
    observations: list[AzureFlowObservation] = []
    for item in tuples:
        fields = item.split(",")
        if len(fields) < 8 or fields[7] == "D":
            continue
        observed_at = _timestamp(fields[0])
        protocol = {"6": "tcp", "17": "udp"}.get(fields[5])
        try:
            source_ip = str(ip_address(fields[1]))
            destination_ip = str(ip_address(fields[2]))
            destination_port = int(fields[4])
        except ValueError:
            continue
        if observed_at is None or protocol is None or not 1 <= destination_port <= 65535:
            continue
        observations.append(
            AzureFlowObservation(
                source_ip=source_ip,
                destination_ip=destination_ip,
                destination_port=destination_port,
                protocol=protocol,
                observed_at=observed_at,
            )
        )
    return observations


def _safe_azure_error(error: Exception) -> AzureFlowLogError:
    status_code = getattr(error, "status_code", None)
    messages = {
        403: (
            "Azure denied Blob read access. Verify Storage Blob Data Reader for the configured "
            "container."
        ),
        404: "The Azure flow-log storage account, container, or prefix was not found.",
    }
    return AzureFlowLogError(
        messages.get(
            status_code, "Azure could not read the configured Virtual Network Flow Log location."
        )
    )


def fetch_flow_log_observations(
    credentials: AzureCredentials,
    settings: AzureFlowLogSettings,
    known_object_versions: set[str],
) -> AzureFlowLogReadResult:
    """Read unseen, bounded Azure VNet Flow Log blob versions with Entra authentication."""

    try:
        from azure.identity import ClientSecretCredential
        from azure.storage.blob import BlobServiceClient

        credential = ClientSecretCredential(
            tenant_id=credentials.tenant_id,
            client_id=credentials.client_id,
            client_secret=credentials.client_secret.get_secret_value(),
        )
        account_url = f"https://{settings.storage_account_name}.blob.core.windows.net"
        container = BlobServiceClient(account_url, credential=credential).get_container_client(
            settings.container_name
        )
        blobs = list(
            islice(container.list_blobs(name_starts_with=settings.prefix), _MAX_OBJECTS_PER_SYNC)
        )
        candidates = sorted(
            (
                blob
                for blob in blobs[:_MAX_OBJECTS_PER_SYNC]
                if isinstance(getattr(blob, "name", None), str)
                and str(blob.name).endswith(".json")
                and _object_version(str(blob.name), getattr(blob, "etag", None))
                not in known_object_versions
            ),
            key=lambda blob: str(getattr(blob, "last_modified", "")),
        )
        observations: list[AzureFlowObservation] = []
        processed: set[str] = set()
        for blob in candidates:
            size = getattr(blob, "size", None)
            if not isinstance(size, int) or size < 0 or size > _MAX_OBJECT_BYTES:
                raise AzureFlowLogError(
                    "Azure flow-log blob exceeds the size limit or has invalid size."
                )
            content = container.download_blob(blob.name).readall()
            if not isinstance(content, bytes) or len(content) > _MAX_OBJECT_BYTES:
                raise AzureFlowLogError("Azure flow-log blob exceeds the size limit.")
            try:
                document = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AzureFlowLogError("Azure flow-log blob contains invalid JSON.") from error
            observations.extend(parse_flow_log_document(document))
            processed.add(_object_version(blob.name, getattr(blob, "etag", None)))
        return AzureFlowLogReadResult(observations, processed, len(candidates))
    except AzureFlowLogError:
        raise
    except AzureError as error:
        raise _safe_azure_error(error) from error
