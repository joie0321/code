"""Azure VM inventory adapter for the customer-side collector appliance.

The control plane receives normalized inventory only. Azure application credentials
remain in the agent process and are never logged or uploaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import SecretStr

from collector_agent.vcenter import VmRecord


@dataclass(frozen=True)
class AzureCredentials:
    """Ephemeral Microsoft Entra application credentials for one Azure source."""

    tenant_id: str
    client_id: str
    client_secret: SecretStr
    subscription_ids: list[str]


def _resource_rows(result: Any) -> list[dict[str, Any]]:
    """Return Azure Resource Graph rows while rejecting unexpected SDK output."""

    rows = getattr(result, "data", result)
    if isinstance(rows, dict):
        values = rows.get("data", rows.get("value", []))
    else:
        values = rows
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _private_ips(properties: object) -> list[str]:
    if not isinstance(properties, dict):
        return []
    value = properties.get("ipConfigurations")
    if not isinstance(value, list):
        return []
    addresses: set[str] = set()
    for ip_configuration in value:
        if not isinstance(ip_configuration, dict):
            continue
        address = ip_configuration.get("privateIPAddress")
        if isinstance(address, str):
            try:
                addresses.add(str(ip_address(address)))
            except ValueError:
                continue
    return sorted(addresses)


def map_inventory(vm_rows: list[dict[str, Any]], nic_rows: list[dict[str, Any]]) -> list[VmRecord]:
    """Map Resource Graph VM/NIC rows to the control plane's normalized inventory."""

    ips_by_nic_id = {
        str(row.get("id", "")).lower(): _private_ips(row.get("properties"))
        for row in nic_rows
        if isinstance(row.get("id"), str)
    }
    records: list[VmRecord] = []
    for row in vm_rows:
        resource_id = row.get("id")
        name = row.get("name")
        if not isinstance(resource_id, str) or not isinstance(name, str):
            continue
        properties = row.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        network_profile = properties.get("networkProfile")
        if not isinstance(network_profile, dict):
            network_profile = {}
        nic_references = network_profile.get("networkInterfaces")
        nic_ids = (
            [
                str(nic.get("id")).lower()
                for nic in nic_references
                if isinstance(nic, dict) and isinstance(nic.get("id"), str)
            ]
            if isinstance(nic_references, list)
            else []
        )
        ips = sorted({address for nic_id in nic_ids for address in ips_by_nic_id.get(nic_id, [])})
        os_profile = properties.get("storageProfile")
        os_disk = os_profile.get("osDisk") if isinstance(os_profile, dict) else None
        os_type = os_disk.get("osType") if isinstance(os_disk, dict) else None
        extended = properties.get("extended")
        instance_view = extended.get("instanceView") if isinstance(extended, dict) else None
        power_code = (
            instance_view.get("powerState", {}).get("code")
            if isinstance(instance_view, dict)
            else None
        )
        power_state = "poweredOn" if power_code == "PowerState/running" else "poweredOff"
        if not isinstance(power_code, str):
            power_state = "unknown"
        records.append(
            VmRecord(
                vm_uuid=str(uuid5(NAMESPACE_URL, resource_id.lower())),
                name=name,
                hostname=None,
                ips=ips,
                cluster=str(row.get("resourceGroup")) if row.get("resourceGroup") else None,
                folder=str(row.get("subscriptionId")) if row.get("subscriptionId") else None,
                os_name=str(os_type) if os_type else None,
                power_state=power_state,
            )
        )
    return sorted(records, key=lambda record: record.vm_uuid)


def fetch_vms(credentials: AzureCredentials) -> list[VmRecord]:
    """Query selected subscriptions through Azure Resource Graph.

    Imports are local so the appliance remains startable until the Azure connector
    dependencies are installed. SDK errors are intentionally handled by runtime as
    a safe, source-local setup failure.
    """

    from azure.identity import ClientSecretCredential
    from azure.mgmt.resourcegraph import ResourceGraphClient
    from azure.mgmt.resourcegraph.models import QueryRequest

    credential = ClientSecretCredential(
        tenant_id=credentials.tenant_id,
        client_id=credentials.client_id,
        client_secret=credentials.client_secret.get_secret_value(),
    )
    client = ResourceGraphClient(credential)
    vm_query = """
        Resources
        | where type =~ 'microsoft.compute/virtualmachines'
        | project id, name, subscriptionId, resourceGroup, properties
    """
    nic_query = """
        Resources
        | where type =~ 'microsoft.network/networkinterfaces'
        | project id, properties
    """
    options = {"subscriptions": credentials.subscription_ids}
    vm_rows = _resource_rows(client.resources(QueryRequest(query=vm_query, **options)))
    nic_rows = _resource_rows(client.resources(QueryRequest(query=nic_query, **options)))
    return map_inventory(vm_rows, nic_rows)
