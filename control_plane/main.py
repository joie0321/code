"""Authenticated agent enrollment and telemetry ingestion API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_plane.config import ControlPlaneSettings
from control_plane.db import build_engine, build_session_factory, run_migrations
from control_plane.models import (
    Collector,
    Enrollment,
    InventoryBatch,
    InventoryVm,
    InventoryVmState,
    Observation,
    ObservationBatch,
    ReconnectionCode,
)
from control_plane.reporting import (
    connection_report,
    detailed_connection_report,
    migration_waves,
    wave_connection_report,
    wave_summary,
)
from control_plane.schemas import (
    AgentHeartbeat,
    AgentReconnect,
    AgentRegistration,
    AgentRegistrationResult,
    EnrollmentCreate,
    EnrollmentCreated,
    InventoryUpload,
    ObservationUpload,
    ReconnectionCodeCreate,
    ReconnectionCodeCreated,
    UploadResult,
)

_COLLECTOR_OFFLINE_AFTER_SECONDS = 180


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _is_expired(expires_at: datetime) -> bool:
    """Compare timestamps safely when SQLite omits timezone information."""

    normalized = expires_at.replace(tzinfo=UTC) if expires_at.tzinfo is None else expires_at
    return normalized <= _now()


def _collector_status(last_seen_at: datetime) -> str:
    """Derive health from the most recent authenticated agent heartbeat."""

    last_seen = (
        last_seen_at.replace(tzinfo=UTC)
        if last_seen_at.tzinfo is None
        else last_seen_at.astimezone(UTC)
    )
    cutoff = _now() - timedelta(seconds=_COLLECTOR_OFFLINE_AFTER_SECONDS)
    return "offline" if last_seen < cutoff else "online"


def create_app(settings: ControlPlaneSettings | None = None) -> FastAPI:
    """Build an isolated application instance for deployment or testing."""

    configured = settings or ControlPlaneSettings()
    configured.validate_runtime()
    engine = build_engine(configured.database_url_for_engine())
    run_migrations(engine)
    sessions = build_session_factory(engine)

    app = FastAPI(title="Migration Discovery Control Plane", version="0.1.0")
    app.state.settings = configured
    app.state.sessions = sessions

    def get_session() -> Session:
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    def require_admin(
        x_control_plane_key: str = Header(...),
        request: Request = None,
    ) -> None:
        expected = request.app.state.settings.control_plane_admin_api_key.get_secret_value()
        if not compare_digest(_digest(x_control_plane_key), _digest(expected)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    def require_dashboard(
        x_control_plane_key: str = Header(...),
        request: Request = None,
    ) -> None:
        expected = request.app.state.settings.dashboard_api_key().get_secret_value()
        if not compare_digest(_digest(x_control_plane_key), _digest(expected)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    def require_agent(
        x_collector_id: str = Header(...),
        x_agent_token: str = Header(...),
        session: Session = Depends(get_session),
    ) -> Collector:
        collector = session.get(Collector, x_collector_id)
        if collector is None or not compare_digest(
            _digest(x_agent_token), collector.agent_token_digest
        ):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
        return collector

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/admin/enrollments", response_model=EnrollmentCreated)
    def create_enrollment(
        payload: EnrollmentCreate,
        _: None = Depends(require_admin),
        session: Session = Depends(get_session),
    ) -> EnrollmentCreated:
        code = f"vmc_enroll_{token_urlsafe(32)}"
        expires_at = _now() + timedelta(minutes=payload.expires_in_minutes)
        session.add(
            Enrollment(
                id=str(uuid4()),
                tenant_id=payload.tenant_id,
                code_digest=_digest(code),
                expires_at=expires_at,
                created_at=_now(),
            )
        )
        session.commit()
        return EnrollmentCreated(enrollment_code=code, expires_at=expires_at)

    @app.post("/api/v1/agents/register", response_model=AgentRegistrationResult)
    def register_agent(
        payload: AgentRegistration,
        session: Session = Depends(get_session),
    ) -> AgentRegistrationResult:
        enrollment = session.scalar(
            select(Enrollment).where(
                Enrollment.tenant_id == payload.tenant_id,
                Enrollment.code_digest == _digest(payload.enrollment_code),
            )
        )
        if (
            enrollment is None
            or enrollment.used_at is not None
            or _is_expired(enrollment.expires_at)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid enrollment"
            )

        collector_id = str(uuid4())
        agent_token = f"vmc_agent_{token_urlsafe(32)}"
        now = _now()
        enrollment.used_at = now
        session.add(
            Collector(
                id=collector_id,
                tenant_id=payload.tenant_id,
                display_name=payload.display_name,
                agent_token_digest=_digest(agent_token),
                software_version=payload.software_version,
                created_at=now,
                last_seen_at=now,
            )
        )
        session.commit()
        return AgentRegistrationResult(collector_id=collector_id, agent_token=agent_token)

    @app.post(
        "/api/v1/admin/collectors/{collector_id}/reconnection-codes",
        response_model=ReconnectionCodeCreated,
    )
    def create_reconnection_code(
        collector_id: str,
        payload: ReconnectionCodeCreate,
        _: None = Depends(require_admin),
        session: Session = Depends(get_session),
    ) -> ReconnectionCodeCreated:
        if session.get(Collector, collector_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        code = f"vmc_reconnect_{token_urlsafe(32)}"
        expires_at = _now() + timedelta(minutes=payload.expires_in_minutes)
        session.add(
            ReconnectionCode(
                id=str(uuid4()),
                collector_id=collector_id,
                code_digest=_digest(code),
                expires_at=expires_at,
                created_at=_now(),
            )
        )
        session.commit()
        return ReconnectionCodeCreated(reconnection_code=code, expires_at=expires_at)

    @app.delete("/api/v1/admin/collectors/{collector_id}")
    def delete_collector(
        collector_id: str,
        _: None = Depends(require_admin),
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Permanently delete one collector and only its dependent telemetry."""

        collector = session.get(Collector, collector_id)
        if collector is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        try:
            deleted = {
                "observations": session.execute(
                    delete(Observation).where(Observation.collector_id == collector_id)
                ).rowcount
                or 0,
                "observation_batches": session.execute(
                    delete(ObservationBatch).where(ObservationBatch.collector_id == collector_id)
                ).rowcount
                or 0,
                "inventory_vms": session.execute(
                    delete(InventoryVm).where(InventoryVm.collector_id == collector_id)
                ).rowcount
                or 0,
                "inventory_batches": session.execute(
                    delete(InventoryBatch).where(InventoryBatch.collector_id == collector_id)
                ).rowcount
                or 0,
                "reconnection_codes": session.execute(
                    delete(ReconnectionCode).where(ReconnectionCode.collector_id == collector_id)
                ).rowcount
                or 0,
            }
            session.delete(collector)
            session.commit()
        except Exception:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Collector deletion could not be completed. Retry after active uploads finish."
                ),
            ) from None
        return {"collector_id": collector_id, "status": "deleted", "deleted": deleted}

    @app.post("/api/v1/agents/reconnect", response_model=AgentRegistrationResult)
    def reconnect_agent(
        payload: AgentReconnect, session: Session = Depends(get_session)
    ) -> AgentRegistrationResult:
        reconnection = session.scalar(
            select(ReconnectionCode).where(
                ReconnectionCode.collector_id == payload.collector_id,
                ReconnectionCode.code_digest == _digest(payload.reconnection_code),
            )
        )
        collector = session.get(Collector, payload.collector_id)
        if (
            reconnection is None
            or collector is None
            or reconnection.used_at is not None
            or _is_expired(reconnection.expires_at)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid reconnection"
            )
        now = _now()
        consumed = session.execute(
            update(ReconnectionCode)
            .where(ReconnectionCode.id == reconnection.id, ReconnectionCode.used_at.is_(None))
            .values(used_at=now)
        ).rowcount
        if consumed != 1:
            session.rollback()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid reconnection"
            )
        agent_token = f"vmc_agent_{token_urlsafe(32)}"
        collector.agent_token_digest = _digest(agent_token)
        collector.status = "online"
        collector.last_seen_at = now
        next_observation_sequence = (
            session.scalar(
                select(func.max(ObservationBatch.sequence)).where(
                    ObservationBatch.collector_id == collector.id
                )
            )
            or 0
        ) + 1
        next_inventory_sequence = (
            session.scalar(
                select(func.max(InventoryBatch.sequence)).where(
                    InventoryBatch.collector_id == collector.id
                )
            )
            or 0
        ) + 1
        session.commit()
        return AgentRegistrationResult(
            collector_id=collector.id,
            agent_token=agent_token,
            next_observation_sequence=next_observation_sequence,
            next_inventory_sequence=next_inventory_sequence,
        )

    @app.post("/api/v1/agents/heartbeat")
    def agent_heartbeat(
        payload: AgentHeartbeat,
        collector: Collector = Depends(require_agent),
        session: Session = Depends(get_session),
    ) -> dict[str, str]:
        attached = session.merge(collector)
        attached.software_version = payload.software_version
        attached.status = "online"
        attached.last_seen_at = _now()
        session.commit()
        return {"status": "accepted"}

    @app.post("/api/v1/agents/observations", response_model=UploadResult)
    def upload_observations(
        payload: ObservationUpload,
        collector: Collector = Depends(require_agent),
        session: Session = Depends(get_session),
    ) -> UploadResult:
        existing = session.scalar(
            select(ObservationBatch).where(
                ObservationBatch.collector_id == collector.id,
                ObservationBatch.sequence == payload.sequence,
            )
        )
        if existing is not None:
            return UploadResult(accepted=0, duplicate=True)

        now = _now()
        session.add(
            ObservationBatch(
                id=str(uuid4()),
                collector_id=collector.id,
                sequence=payload.sequence,
                received_at=now,
            )
        )
        for item in payload.observations:
            session.add(
                Observation(
                    id=str(uuid4()),
                    collector_id=collector.id,
                    source_vm_uuid=item.source_vm_uuid,
                    source_ip=item.source_ip,
                    destination_ip=item.destination_ip,
                    destination_port=item.destination_port,
                    protocol=item.protocol,
                    collector_type=item.collector_type,
                    observed_at=item.observed_at,
                    process=item.process,
                )
            )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return UploadResult(accepted=0, duplicate=True)
        return UploadResult(accepted=len(payload.observations), duplicate=False)

    @app.post("/api/v1/agents/inventory", response_model=UploadResult)
    def upload_inventory(
        payload: InventoryUpload,
        collector: Collector = Depends(require_agent),
        session: Session = Depends(get_session),
    ) -> UploadResult:
        existing = session.scalar(
            select(InventoryBatch).where(
                InventoryBatch.collector_id == collector.id,
                InventoryBatch.sequence == payload.sequence,
            )
        )
        if existing is not None:
            return UploadResult(accepted=0, duplicate=True)
        now = _now()
        active_vms = session.scalars(
            select(InventoryVm).where(
                InventoryVm.collector_id == collector.id,
                InventoryVm.is_active.is_(True),
            )
        ).all()
        present_vm_uuids = {item.vm_uuid for item in payload.vms}
        for vm in active_vms:
            vm.is_active = False
            if vm.vm_uuid not in present_vm_uuids:
                session.add(
                    InventoryVmState(
                        id=str(uuid4()),
                        collector_id=collector.id,
                        vm_uuid=vm.vm_uuid,
                        name=vm.name,
                        ips=vm.ips,
                        power_state=vm.power_state,
                        is_active=False,
                        captured_at=now,
                    )
                )
        for item in payload.vms:
            vm = session.scalar(
                select(InventoryVm).where(
                    InventoryVm.collector_id == collector.id,
                    InventoryVm.vm_uuid == item.vm_uuid,
                )
            )
            if vm is None:
                vm = InventoryVm(id=str(uuid4()), collector_id=collector.id, vm_uuid=item.vm_uuid)
                session.add(vm)
            vm.name = item.name
            vm.hostname = item.hostname
            vm.ips = ",".join(item.ips)
            vm.cluster = item.cluster
            vm.folder = item.folder
            vm.os_name = item.os_name
            vm.power_state = item.power_state
            vm.is_active = True
            vm.updated_at = now
            session.add(
                InventoryVmState(
                    id=str(uuid4()),
                    collector_id=collector.id,
                    vm_uuid=vm.vm_uuid,
                    name=vm.name,
                    ips=vm.ips,
                    power_state=vm.power_state,
                    is_active=True,
                    captured_at=now,
                )
            )
        session.add(
            InventoryBatch(
                id=str(uuid4()),
                collector_id=collector.id,
                sequence=payload.sequence,
                received_at=now,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = session.scalar(
                select(InventoryBatch).where(
                    InventoryBatch.collector_id == collector.id,
                    InventoryBatch.sequence == payload.sequence,
                )
            )
            if existing is not None:
                return UploadResult(accepted=0, duplicate=True)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Inventory snapshot contains conflicting VM identifiers.",
            ) from None
        return UploadResult(accepted=len(payload.vms), duplicate=False)

    @app.get("/api/v1/dashboard/collectors", dependencies=[Depends(require_dashboard)])
    def list_collectors(session: Session = Depends(get_session)) -> list[dict[str, object]]:
        collectors = session.scalars(select(Collector).order_by(Collector.display_name)).all()
        return [
            {
                "collector_id": collector.id,
                "tenant_id": collector.tenant_id,
                "display_name": collector.display_name,
                "status": _collector_status(collector.last_seen_at),
                "software_version": collector.software_version,
                "last_seen_at": collector.last_seen_at.isoformat(),
            }
            for collector in collectors
        ]

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/inventory",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_inventory(
        collector_id: str, session: Session = Depends(get_session)
    ) -> list[dict[str, object]]:
        collector = session.get(Collector, collector_id)
        if collector is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        vms = session.scalars(
            select(InventoryVm)
            .where(InventoryVm.collector_id == collector_id, InventoryVm.is_active.is_(True))
            .order_by(InventoryVm.name)
        ).all()
        return [
            {
                "vm_name": vm.name,
                "vm_uuid": vm.vm_uuid,
                "hostname": vm.hostname,
                "ips": vm.ips,
                "power_state": vm.power_state,
                "cluster": vm.cluster,
                "folder": vm.folder,
                "os_name": vm.os_name,
                "updated_at": vm.updated_at.isoformat(),
            }
            for vm in vms
        ]

    def _require_collector(collector_id: str, session: Session) -> None:
        if session.get(Collector, collector_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/connections",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_connections(
        collector_id: str,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
        limit: int = Query(default=500, ge=1, le=1000),
    ) -> list[dict[str, object]]:
        _require_collector(collector_id, session)
        return connection_report(session, collector_id, observed_after, observed_before, limit)

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/wave-connections",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_wave_connections(
        collector_id: str,
        vm_uuid: list[str] = Query(min_length=1, max_length=250),
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=100, ge=1, le=250),
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        return wave_connection_report(
            session,
            collector_id,
            vm_uuid,
            observed_after,
            observed_before,
            page,
            page_size,
        )

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/detailed-connections",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_detailed_connections(
        collector_id: str,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> list[dict[str, object]]:
        _require_collector(collector_id, session)
        return detailed_connection_report(session, collector_id, observed_after, observed_before)

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/waves",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_waves(
        collector_id: str,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> list[dict[str, object]]:
        _require_collector(collector_id, session)
        return migration_waves(session, collector_id, observed_after, observed_before)

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/wave-summary",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_wave_summary(
        collector_id: str,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> dict[str, int]:
        _require_collector(collector_id, session)
        return wave_summary(session, collector_id, observed_after, observed_before)

    return app
