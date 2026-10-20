"""Boundary validation for control-plane administrative and agent requests."""

from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class WaveGroupingPort(BaseModel):
    """One explicitly approved application port in the dynamic/private range."""

    protocol: Literal["tcp", "udp"]
    port: int = Field(ge=32768, le=65535)


class WaveGroupingPortAllowlistUpdate(BaseModel):
    ports: list[WaveGroupingPort] = Field(default_factory=list, max_length=128)

    @field_validator("ports")
    @classmethod
    def require_unique_ports(cls, ports: list[WaveGroupingPort]) -> list[WaveGroupingPort]:
        values = [(entry.protocol, entry.port) for entry in ports]
        if len(values) != len(set(values)):
            raise ValueError("each protocol and port combination can be approved only once")
        return ports


class EnrollmentCreate(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    expires_in_minutes: int = Field(default=15, ge=5, le=60)


class EnrollmentCreated(BaseModel):
    enrollment_code: str
    expires_at: datetime


class ReconnectionCodeCreate(BaseModel):
    expires_in_minutes: int = Field(default=15, ge=5, le=60)


class ReconnectionCodeCreated(BaseModel):
    reconnection_code: str
    expires_at: datetime


class AgentRegistration(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    enrollment_code: str = Field(min_length=24, max_length=256)
    display_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9 ._-]+$")
    software_version: str = Field(min_length=1, max_length=64)


class AgentRegistrationResult(BaseModel):
    collector_id: str
    agent_token: str
    next_observation_sequence: int = Field(default=1, ge=1)
    next_inventory_sequence: int = Field(default=1, ge=1)


class AgentReconnect(BaseModel):
    collector_id: str = Field(min_length=36, max_length=36)
    reconnection_code: str = Field(min_length=24, max_length=256)


class AgentHeartbeat(BaseModel):
    software_version: str = Field(min_length=1, max_length=64)
    inventory_vm_count: int = Field(ge=0, le=100_000)


class ConnectionObservation(BaseModel):
    source_vm_uuid: str = Field(min_length=1, max_length=64)
    source_ip: str
    destination_ip: str
    destination_port: int = Field(ge=1, le=65535)
    protocol: str = Field(default="tcp", pattern=r"^(tcp|udp)$")
    collector_type: str = Field(pattern=r"^(ipfix|guest|aws_vpc_flow_logs|azure_vnet_flow_logs)$")
    observed_at: datetime
    process: str | None = Field(default=None, max_length=512)

    @field_validator("source_ip", "destination_ip")
    @classmethod
    def require_ip_address(cls, value: str) -> str:
        try:
            return str(ip_address(value))
        except ValueError as error:
            raise ValueError("must be an IPv4 or IPv6 address") from error


class ObservationUpload(BaseModel):
    sequence: int = Field(ge=1)
    observations: list[ConnectionObservation] = Field(min_length=1, max_length=5_000)


class UploadResult(BaseModel):
    accepted: int
    duplicate: bool
    next_sequence: int | None = Field(default=None, ge=1)


class InventoryVmIn(BaseModel):
    vm_uuid: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    hostname: str | None = Field(default=None, max_length=255)
    ips: list[str] = Field(default_factory=list, max_length=64)
    cluster: str | None = Field(default=None, max_length=255)
    folder: str | None = Field(default=None, max_length=1024)
    os_name: str | None = Field(default=None, max_length=512)
    power_state: str | None = Field(default=None, max_length=32)

    @field_validator("ips")
    @classmethod
    def normalize_ips(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            try:
                normalized.append(str(ip_address(value)))
            except ValueError as error:
                raise ValueError("ips must contain IPv4 or IPv6 addresses") from error
        return sorted(set(normalized))


class InventoryUpload(BaseModel):
    sequence: int = Field(ge=1)
    vms: list[InventoryVmIn] = Field(max_length=100_000)


class InventoryRemovalConfirmation(BaseModel):
    vm_uuids: list[str] = Field(min_length=1, max_length=100_000)
    planner_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9 ._-]+$")
    note: str | None = Field(default=None, max_length=2000)
