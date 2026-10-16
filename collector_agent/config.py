"""Configuration for the customer-side collector runtime."""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_COLLECTOR_DIRECTORY = Path(__file__).resolve().parent


class AgentSettings(BaseSettings):
    """Non-secret settings. vCenter and guest credentials are never configured here."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    agent_bind_host: str = "127.0.0.1"
    agent_bind_port: int = 8443
    agent_heartbeat_interval_seconds: int = 60
    inventory_sync_interval_seconds: int = 86_400
    agent_sequence_state_file: Path = _COLLECTOR_DIRECTORY / "collector_sequence_state.json"

    @field_validator("agent_heartbeat_interval_seconds")
    @classmethod
    def bound_heartbeat_interval(cls, value: int) -> int:
        if not 30 <= value <= 3600:
            raise ValueError("AGENT_HEARTBEAT_INTERVAL_SECONDS must be between 30 and 3600")
        return value

    @field_validator("inventory_sync_interval_seconds")
    @classmethod
    def bound_inventory_sync_interval(cls, value: int) -> int:
        if not 300 <= value <= 604_800:
            raise ValueError("INVENTORY_SYNC_INTERVAL_SECONDS must be between 300 and 604800")
        return value

    def validate_runtime(self) -> None:
        if self.app_env != "development" and self.agent_bind_host != "127.0.0.1":
            raise ValueError(
                "AGENT_BIND_HOST must remain 127.0.0.1 until local setup TLS is implemented"
            )
