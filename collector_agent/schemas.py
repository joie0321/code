"""Local-only setup API request validation."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator


class RegistrationRequest(BaseModel):
    control_plane_url: str = Field(min_length=8, max_length=2048)
    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    enrollment_code: SecretStr = Field(min_length=24, max_length=256)
    display_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9 ._-]+$")


class ReconnectRequest(BaseModel):
    control_plane_url: str = Field(min_length=8, max_length=2048)
    collector_id: str = Field(min_length=36, max_length=36)
    reconnection_code: SecretStr = Field(min_length=24, max_length=256)
    display_name: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9 ._-]+$")


class LocalCredentialsRequest(BaseModel):
    collector_id: str = Field(min_length=36, max_length=36)
    vcenter_host: str = Field(min_length=1, max_length=255)
    vcenter_username: str = Field(min_length=1, max_length=255)
    vcenter_password: SecretStr = Field(min_length=1, max_length=1024)
    guest_username: str | None = Field(default=None, max_length=255)
    guest_password: SecretStr | None = Field(default=None, max_length=1024)
    verify_tls: bool = True


class LocalObservation(BaseModel):
    source_vm_uuid: str = Field(min_length=1, max_length=64)
    source_ip: str = Field(min_length=3, max_length=45)
    destination_ip: str = Field(min_length=3, max_length=45)
    destination_port: int = Field(ge=1, le=65535)
    collector_type: str = Field(pattern=r"^(ipfix|guest)$")
    observed_at: str = Field(min_length=20, max_length=40)
    process: str | None = Field(default=None, max_length=512)


class IpfixStartRequest(BaseModel):
    collector_id: str = Field(min_length=36, max_length=36)
    selected_clusters: list[str] = Field(default_factory=list, max_length=128)
    manual_exporters: list[str] = Field(default_factory=list, max_length=128)
    excluded_exporters: list[str] = Field(default_factory=list, max_length=256)

    @field_validator("selected_clusters")
    @classmethod
    def unique_clusters(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator("manual_exporters", "excluded_exporters")
    @classmethod
    def individual_ip_addresses_only(cls, value: list[str]) -> list[str]:
        from ipaddress import ip_address

        try:
            return list(
                dict.fromkeys(str(ip_address(item.strip())) for item in value if item.strip())
            )
        except ValueError as error:
            raise ValueError("IPFIX exporter addresses must be individual IP addresses") from error
