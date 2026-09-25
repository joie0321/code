"""Local-only setup API request validation."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr, field_validator


class RegistrationRequest(BaseModel):
    control_plane_url: str = Field(min_length=8, max_length=2048)
    tenant_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    enrollment_code: SecretStr = Field(min_length=24, max_length=256)
    display_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9 ._-]+$")
    source_type: str = Field(default="vmware", pattern=r"^(vmware|azure|aws)$")


class ReconnectRequest(BaseModel):
    control_plane_url: str = Field(min_length=8, max_length=2048)
    collector_id: str = Field(min_length=36, max_length=36)
    reconnection_code: SecretStr = Field(min_length=24, max_length=256)
    display_name: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9 ._-]+$")
    source_type: str = Field(default="vmware", pattern=r"^(vmware|azure|aws)$")


class LocalCredentialsRequest(BaseModel):
    collector_id: str = Field(min_length=36, max_length=36)
    vcenter_host: str = Field(min_length=1, max_length=255)
    vcenter_username: str = Field(min_length=1, max_length=255)
    vcenter_password: SecretStr = Field(min_length=1, max_length=1024)
    guest_username: str | None = Field(default=None, max_length=255)
    guest_password: SecretStr | None = Field(default=None, max_length=1024)
    verify_tls: bool = True


class AzureCredentialsRequest(BaseModel):
    """In-memory Azure application credentials for a selected Azure source."""

    collector_id: str = Field(min_length=36, max_length=36)
    azure_tenant_id: str = Field(min_length=1, max_length=255)
    azure_client_id: str = Field(min_length=1, max_length=255)
    azure_client_secret: SecretStr = Field(min_length=1, max_length=1024)
    subscription_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("subscription_ids")
    @classmethod
    def unique_subscription_ids(cls, value: list[str]) -> list[str]:
        subscriptions = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        if not subscriptions:
            raise ValueError("at least one Azure subscription ID is required")
        if any(len(item) > 255 for item in subscriptions):
            raise ValueError("Azure subscription IDs must be at most 255 characters")
        return subscriptions


class AwsCredentialsRequest(BaseModel):
    """Local AWS inventory authentication for a selected AWS source."""

    collector_id: str = Field(min_length=36, max_length=36)
    aws_region: str = Field(min_length=3, max_length=32, pattern=r"^[a-z0-9-]+$")
    aws_access_key_id: str | None = Field(default=None, min_length=16, max_length=256)
    aws_secret_access_key: SecretStr | None = Field(default=None, min_length=16, max_length=1024)
    aws_session_token: SecretStr | None = Field(default=None, min_length=16, max_length=4096)

    @field_validator("aws_region")
    @classmethod
    def normalized_region(cls, value: str) -> str:
        return value.strip().lower()

    def model_post_init(self, __context: object) -> None:
        if bool(self.aws_access_key_id) != bool(self.aws_secret_access_key):
            raise ValueError("provide both AWS access key ID and secret access key, or neither")
        if self.aws_session_token and not self.aws_access_key_id:
            raise ValueError("an AWS session token requires an access key ID and secret access key")


class AwsFlowLogSettingsRequest(BaseModel):
    """S3 location of VPC Flow Logs for a configured AWS source."""

    collector_id: str = Field(min_length=36, max_length=36)
    s3_bucket_name: str = Field(
        min_length=3, max_length=63, pattern=r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$"
    )
    s3_prefix: str = Field(min_length=1, max_length=1024)

    @field_validator("s3_prefix")
    @classmethod
    def normalized_prefix(cls, value: str) -> str:
        prefix = value.strip().lstrip("/")
        if not prefix or ".." in prefix or "\\" in prefix:
            raise ValueError("S3 prefix must be a non-empty object prefix")
        return prefix if prefix.endswith("/") else f"{prefix}/"


class LocalObservation(BaseModel):
    source_vm_uuid: str = Field(min_length=1, max_length=64)
    source_ip: str = Field(min_length=3, max_length=45)
    destination_ip: str = Field(min_length=3, max_length=45)
    destination_port: int = Field(ge=1, le=65535)
    collector_type: str = Field(pattern=r"^(ipfix|guest|aws_vpc_flow_logs)$")
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
