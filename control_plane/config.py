"""Secure runtime configuration for the OCI control plane."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _project_sqlite_url(filename: str) -> str:
    """Keep development SQLite storage independent of the launch directory."""

    return f"sqlite:///{(_PROJECT_ROOT / filename).as_posix()}"


class ControlPlaneSettings(BaseSettings):
    """Configuration loaded from environment variables, never from agent payloads."""

    model_config = SettingsConfigDict(env_file=_PROJECT_ROOT / ".env", extra="ignore")

    app_env: str = "development"
    database_mode: Literal["development", "oci_vault", "external", "bundled"] | None = None
    database_url: str = _project_sqlite_url("control_plane.db")
    database_password: SecretStr | None = None
    database_password_secret_ocid: str | None = None
    database_password_file: Path | None = None
    control_plane_admin_api_key: SecretStr | None = None
    control_plane_admin_api_key_secret_ocid: str | None = None
    control_plane_dashboard_api_url: str = "http://127.0.0.1:8100"
    control_plane_dashboard_api_key: SecretStr | None = None
    control_plane_dashboard_api_key_secret_ocid: str | None = None
    control_plane_dashboard_api_local_loopback: bool = False

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
    def require_strong_admin_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        if len(value.get_secret_value()) < 32:
            raise ValueError("CONTROL_PLANE_ADMIN_API_KEY must be at least 32 characters")
        return value

    def validate_runtime(self) -> None:
        mode = self.effective_database_mode()
        if mode == "bundled":
            raise ValueError(
                "DATABASE_MODE=bundled is not available until the bundled database phase"
            )
        if mode == "development":
            if self.app_env != "development":
                raise ValueError("DATABASE_MODE=development requires APP_ENV=development")
            return

        database = make_url(self.database_url)
        if database.drivername != "postgresql+psycopg":
            raise ValueError("Production DATABASE_URL must use postgresql+psycopg")
        if database.password is not None or self.database_password is not None:
            raise ValueError(
                "Do not place a database password in DATABASE_URL or DATABASE_PASSWORD "
                "outside development."
            )
        if database.query.get("sslmode") != "verify-full" or not database.query.get("sslrootcert"):
            raise ValueError("Production DATABASE_URL must set sslmode=verify-full and sslrootcert")
        self._validate_api_key_sources()
        self._validate_dashboard_api_endpoint()
        if mode == "oci_vault":
            if not self.database_password_secret_ocid:
                raise ValueError(
                    "DATABASE_PASSWORD_SECRET_OCID is required for DATABASE_MODE=oci_vault"
                )
            if self.database_password_file is not None:
                raise ValueError(
                    "DATABASE_PASSWORD_FILE is not permitted for DATABASE_MODE=oci_vault"
                )
        elif mode == "external":
            self._read_database_password_file()

    def admin_api_key(self) -> SecretStr:
        """Read the administrative API key without exposing it in configuration."""

        if self.control_plane_admin_api_key is not None:
            return self.control_plane_admin_api_key
        if self.control_plane_admin_api_key_secret_ocid is None:
            raise ValueError("A Control Plane administrative API key source is required")
        return SecretStr(
            self._read_oci_vault_secret(
                self.control_plane_admin_api_key_secret_ocid,
                "Control Plane administrative API key",
            )
        )

    def dashboard_api_key(self) -> SecretStr:
        """Use a separate read-only dashboard key in production."""

        if self.control_plane_dashboard_api_key is not None:
            return self.control_plane_dashboard_api_key
        if self.control_plane_dashboard_api_key_secret_ocid is not None:
            return SecretStr(
                self._read_oci_vault_secret(
                    self.control_plane_dashboard_api_key_secret_ocid,
                    "Control Plane dashboard API key",
                )
            )
        return self.admin_api_key()

    def database_url_for_engine(self) -> str:
        """Return a SQLAlchemy URL with a password supplied outside the URL itself.

        OCI production deployments retrieve the password at startup using the instance
        principal. Development can inject a transient password through the process
        environment for local PostgreSQL testing.
        """

        database = make_url(self.database_url)
        if database.drivername.startswith("sqlite"):
            return self.database_url
        password = self._database_password()
        return database.set(password=password).render_as_string(hide_password=False)

    def _database_password(self) -> str:
        mode = self.effective_database_mode()
        if mode == "development" and self.database_password is not None:
            return self.database_password.get_secret_value()
        if mode == "external":
            return self._read_database_password_file()
        if mode != "oci_vault" or not self.database_password_secret_ocid:
            raise ValueError("A database password source is required for PostgreSQL")
        return self._read_oci_vault_secret(
            self.database_password_secret_ocid,
            "database password",
        )

    def effective_database_mode(self) -> Literal["development", "oci_vault", "external", "bundled"]:
        """Resolve a mode while preserving existing OCI deployment configuration."""

        if self.database_mode is not None:
            return self.database_mode
        return "development" if self.app_env == "development" else "oci_vault"

    def _read_database_password_file(self) -> str:
        """Read a bounded deployment secret without exposing its contents in errors."""

        if self.database_password_file is None:
            raise ValueError("DATABASE_PASSWORD_FILE is required for DATABASE_MODE=external")
        path = self.database_password_file
        if not path.is_absolute():
            raise ValueError("DATABASE_PASSWORD_FILE must be an absolute path")
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError("Unable to read DATABASE_PASSWORD_FILE") from error
        password = content.rstrip("\r\n")
        if not password or "\x00" in password or len(password) > 4096:
            raise ValueError("DATABASE_PASSWORD_FILE contains an invalid password value")
        return password

    @staticmethod
    def _read_oci_vault_secret(secret_ocid: str, description: str) -> str:
        """Read a single Vault secret through the OCI Container Instance identity."""

        try:
            import oci

            signer = oci.auth.signers.get_resource_principals_signer()
            client = oci.secrets.SecretsClient(config={}, signer=signer)
            bundle = client.get_secret_bundle(secret_id=secret_ocid).data
            content = bundle.secret_bundle_content.content
            value = base64.b64decode(content).decode("utf-8")
        except Exception as error:
            raise ValueError(
                f"Unable to retrieve the {description} from OCI Vault using the "
                "Container Instance resource principal. Check the dynamic-group policy "
                "and secret OCID."
            ) from error
        if not value or "\x00" in value or len(value) > 4096:
            raise ValueError(f"OCI Vault returned an invalid {description}")
        return value

    def _validate_api_key_sources(self) -> None:
        if self.app_env == "development":
            if self.control_plane_admin_api_key is None:
                raise ValueError("CONTROL_PLANE_ADMIN_API_KEY is required")
            return
        direct_key_supplied = (
            self.control_plane_admin_api_key is not None
            or self.control_plane_dashboard_api_key is not None
        )
        if direct_key_supplied:
            raise ValueError(
                "Use CONTROL_PLANE_ADMIN_API_KEY_SECRET_OCID and "
                "CONTROL_PLANE_DASHBOARD_API_KEY_SECRET_OCID outside development"
            )
        if (
            not self.control_plane_admin_api_key_secret_ocid
            or not self.control_plane_dashboard_api_key_secret_ocid
        ):
            raise ValueError(
                "Separate administrative and dashboard API key secret OCIDs are required "
                "outside development"
            )

    def _validate_dashboard_api_endpoint(self) -> None:
        """Allow HTTP only for a local development dashboard-to-API connection."""

        endpoint = urlsplit(self.control_plane_dashboard_api_url)
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("CONTROL_PLANE_DASHBOARD_API_URL must be a plain API origin")
        is_loopback = endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
        if self.app_env == "development":
            if endpoint.scheme not in {"http", "https"} or endpoint.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError(
                    "Development external PostgreSQL mode permits the dashboard API only on "
                    "localhost, 127.0.0.1, or ::1"
                )
            return
        if self.control_plane_dashboard_api_local_loopback:
            if endpoint.scheme != "http" or not is_loopback:
                raise ValueError(
                    "CONTROL_PLANE_DASHBOARD_API_LOCAL_LOOPBACK requires an HTTP loopback "
                    "CONTROL_PLANE_DASHBOARD_API_URL"
                )
            if self.control_plane_dashboard_api_key_secret_ocid is None:
                raise ValueError(
                    "CONTROL_PLANE_DASHBOARD_API_KEY_SECRET_OCID is required outside development"
                )
            return
        if endpoint.scheme != "https":
            raise ValueError("CONTROL_PLANE_DASHBOARD_API_URL must use HTTPS outside development")
        if self.control_plane_dashboard_api_key_secret_ocid is None:
            raise ValueError(
                "CONTROL_PLANE_DASHBOARD_API_KEY_SECRET_OCID is required outside development"
            )
