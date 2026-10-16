"""AWS EC2 inventory adapter for the customer-side collector appliance.

AWS credentials remain in the local agent process. The control plane receives
only normalized inventory records and never receives AWS credentials or tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import SecretStr

from collector_agent.vcenter import VmRecord


@dataclass(frozen=True)
class AwsCredentials:
    """Ephemeral AWS credential configuration for one account and Region source."""

    region_name: str
    access_key_id: str | None = None
    secret_access_key: SecretStr | None = None
    session_token: SecretStr | None = None


def aws_session(credentials: AwsCredentials) -> Any:
    """Create an AWS SDK session without persisting or logging credentials."""

    import boto3

    session_kwargs: dict[str, str] = {"region_name": credentials.region_name}
    if credentials.access_key_id and credentials.secret_access_key:
        session_kwargs.update(
            {
                "aws_access_key_id": credentials.access_key_id,
                "aws_secret_access_key": credentials.secret_access_key.get_secret_value(),
            }
        )
        if credentials.session_token:
            session_kwargs["aws_session_token"] = credentials.session_token.get_secret_value()
    return boto3.Session(**session_kwargs)


def _valid_ips(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    addresses: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            addresses.add(str(ip_address(value)))
        except ValueError:
            continue
    return sorted(addresses)


def _instance_ips(instance: dict[str, Any]) -> list[str]:
    """Return private interface addresses, rejecting malformed API data."""

    values: list[str] = []
    private_ip = instance.get("PrivateIpAddress")
    if isinstance(private_ip, str):
        values.append(private_ip)
    interfaces = instance.get("NetworkInterfaces")
    if isinstance(interfaces, list):
        for interface in interfaces:
            if not isinstance(interface, dict):
                continue
            addresses = interface.get("PrivateIpAddresses")
            if isinstance(addresses, list):
                values.extend(
                    item["PrivateIpAddress"]
                    for item in addresses
                    if isinstance(item, dict) and isinstance(item.get("PrivateIpAddress"), str)
                )
    return _valid_ips(values)


def _tag_value(tags: object, name: str) -> str | None:
    if not isinstance(tags, list):
        return None
    for tag in tags:
        if isinstance(tag, dict) and tag.get("Key") == name and isinstance(tag.get("Value"), str):
            return tag["Value"]
    return None


def map_inventory(instances: list[dict[str, Any]], region_name: str) -> list[VmRecord]:
    """Map AWS EC2 responses to the normalized inventory contract."""

    records: list[VmRecord] = []
    for instance in instances:
        instance_id = instance.get("InstanceId")
        if not isinstance(instance_id, str) or not instance_id:
            continue
        state = instance.get("State")
        state_name = state.get("Name") if isinstance(state, dict) else None
        power_state = "poweredOn" if state_name == "running" else "poweredOff"
        if state_name not in {"running", "stopped", "stopping", "shutting-down", "terminated"}:
            power_state = "unknown"
        platform = "Windows" if instance.get("Platform") == "windows" else "Linux/Unix"
        name = _tag_value(instance.get("Tags"), "Name") or instance_id
        vpc_id = instance.get("VpcId")
        records.append(
            VmRecord(
                vm_uuid=str(
                    uuid5(NAMESPACE_URL, f"aws:{region_name.lower()}:{instance_id.lower()}")
                ),
                name=name,
                hostname=None,
                ips=_instance_ips(instance),
                cluster=str(vpc_id) if isinstance(vpc_id, str) else None,
                folder=region_name,
                os_name=platform,
                power_state=power_state,
            )
        )
    return sorted(records, key=lambda record: record.vm_uuid)


def fetch_vms(credentials: AwsCredentials) -> list[VmRecord]:
    """Fetch EC2 inventory using local credentials or the AWS default provider chain.

    The default chain supports appliance-hosted IAM Roles Anywhere through an AWS
    SDK credential process. AWS SDK exceptions are deliberately handled by the
    runtime as a source-local, non-secret error.
    """

    client = aws_session(credentials).client("ec2")
    instances = [
        instance
        for page in client.get_paginator("describe_instances").paginate()
        for reservation in page.get("Reservations", [])
        if isinstance(reservation, dict)
        for instance in reservation.get("Instances", [])
        if isinstance(instance, dict)
    ]
    return map_inventory(instances, credentials.region_name)
