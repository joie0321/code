"""Secure runtime configuration for the OCI control plane."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _project_sqlite_url(filename: str) -> str:
    """Keep development SQLite storage independent of the launch directory."""

    return f"sqlite:///{(_PROJECT_ROOT / filename).as_posix()}"


class ControlPlaneSettings(BaseSettings):
    """Configuration loaded from environment variables, never from agent payloads."""

    model_config = SettingsConfigDict(env_file=_PROJECT_ROOT / ".env", extra="ignore")

    app_env: str = "development"
    database_url: str = _project_sqlite_url("control_plane.db")
    control_plane_admin_api_key: SecretStr
    control_plane_dashboard_api_url: str = "http://127.0.0.1:8100"
    control_plane_dashboard_api_key: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def resolve_project_relative_sqlite_url(cls, value: str) -> str:
        """Avoid split SQLite databases when API and UI start from different folders."""

        prefix = "sqlite:///./"
        if value.startswith(prefix):
            return _project_sqlite_url(value.removeprefix(prefix))
        return value

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
