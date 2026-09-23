"""Secure runtime configuration for the OCI control plane."""

from __future__ import annotations

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlPlaneSettings(BaseSettings):
    """Configuration loaded from environment variables, never from agent payloads."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "sqlite:///./control_plane.db"
    control_plane_admin_api_key: SecretStr
    control_plane_dashboard_api_url: str = "http://127.0.0.1:8100"
    control_plane_dashboard_api_key: SecretStr | None = None

    @field_validator("control_plane_admin_api_key")
    @classmethod
    def require_strong_admin_key(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("CONTROL_PLANE_ADMIN_API_KEY must be at least 32 characters")
        return value

    def validate_runtime(self) -> None:
        if self.app_env != "development" and self.database_url.startswith("sqlite"):
            raise ValueError("SQLite is permitted only in APP_ENV=development")
        if self.app_env != "development":
            if not self.control_plane_dashboard_api_url.startswith("https://"):
                raise ValueError(
                    "CONTROL_PLANE_DASHBOARD_API_URL must use HTTPS outside development"
                )
            if self.control_plane_dashboard_api_key is None:
                raise ValueError("CONTROL_PLANE_DASHBOARD_API_KEY is required outside development")

    def dashboard_api_key(self) -> SecretStr:
        """Use a separate read-only dashboard key in production."""

        return self.control_plane_dashboard_api_key or self.control_plane_admin_api_key
