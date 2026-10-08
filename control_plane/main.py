"""Authenticated agent enrollment and telemetry ingestion API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from control_plane.config import ControlPlaneSettings
from control_plane.db import build_engine, build_session_factory, run_migrations
from control_plane.models import (
    Collector,
    DependencyDecision,
    Enrollment,
    InventoryBatch,
    InventoryVm,
    InventoryVmState,
    MigrationPlan,
    MigrationPlanDependency,
    MigrationPlanVm,
    Observation,
    ObservationBatch,
    ReconnectionCode,
)
from control_plane.reporting import (
    connection_report,
    dependency_review_report,
    detailed_connection_report,
    migration_waves,
    plan_dependency_report,
    plan_drift_report,
    wave_connection_report,
    wave_readiness_report,
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


class DependencyDecisionPayload(BaseModel):
    source_vm_uuid: str = Field(min_length=1, max_length=64)
    destination_identity: str = Field(min_length=1, max_length=64)
    protocol: str = Field(min_length=1, max_length=8)
    port_key: str = Field(min_length=1, max_length=32)
    category: str = Field(min_length=1, max_length=64)
    decision: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=2000)


class DependencyDecisionReference(BaseModel):
    """A normalized dependency identity selected for a common planner decision."""

    source_vm_uuid: str = Field(min_length=1, max_length=64)
    destination_identity: str = Field(min_length=1, max_length=64)
    protocol: str = Field(min_length=1, max_length=8)
    port_key: str = Field(min_length=1, max_length=32)
    category: str = Field(min_length=1, max_length=64)


class DependencyDecisionBatchPayload(BaseModel):
    dependencies: list[DependencyDecisionReference] = Field(min_length=1, max_length=250)
    decision: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=2000)


class MigrationPlanCreatePayload(BaseModel):
    planner_name: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)


class MigrationPlanAssignmentPayload(BaseModel):
    vm_uuid: str = Field(min_length=1, max_length=64)
    wave_number: int | None = Field(default=None, ge=1, le=10_000)
    disposition: str = Field(min_length=1, max_length=16)
    note: str | None = Field(default=None, max_length=2000)


class MigrationPlanBulkAssignmentPayload(BaseModel):
    vm_uuids: list[str] = Field(min_length=1, max_length=250)
    wave_number: int | None = Field(default=None, ge=1, le=10_000)
    disposition: str = Field(min_length=1, max_length=16)
    note: str | None = Field(default=None, max_length=2000)


class MigrationPlanApprovalPayload(BaseModel):
    planner_name: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=2000)


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
            plan_ids = select(MigrationPlan.id).where(MigrationPlan.collector_id == collector_id)
            deleted = {
                "migration_plan_dependencies": session.execute(
                    delete(MigrationPlanDependency).where(MigrationPlanDependency.plan_id.in_(plan_ids))
                ).rowcount
                or 0,
                "migration_plan_vms": session.execute(
                    delete(MigrationPlanVm).where(MigrationPlanVm.plan_id.in_(plan_ids))
                ).rowcount
                or 0,
                "migration_plans": session.execute(
                    delete(MigrationPlan).where(MigrationPlan.collector_id == collector_id)
                ).rowcount
                or 0,
                "dependency_decisions": session.execute(
                    delete(DependencyDecision).where(DependencyDecision.collector_id == collector_id)
                ).rowcount
                or 0,
                "inventory_vm_states": session.execute(
                    delete(InventoryVmState).where(InventoryVmState.collector_id == collector_id)
                ).rowcount
                or 0,
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
        deduplicate: bool = False,
        exclude_dynamic_private_ports: bool = False,
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
            deduplicate,
            exclude_dynamic_private_ports,
        )

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/wave-readiness",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_wave_readiness(
        collector_id: str,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
        use_approved_plan: bool = True,
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        return wave_readiness_report(
            session,
            collector_id,
            observed_after,
            observed_before,
            use_approved_plan=use_approved_plan,
        )

    @app.get("/api/v1/dashboard/collectors/{collector_id}/plan-drift", dependencies=[Depends(require_dashboard)])
    def collector_plan_drift(collector_id: str, session: Session = Depends(get_session)) -> dict[str, object]:
        _require_collector(collector_id, session)
        return plan_drift_report(session, collector_id)

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/dependencies",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_dependencies(
        collector_id: str,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        report = dependency_review_report(session, collector_id, observed_after, observed_before)
        decisions = session.scalars(
            select(DependencyDecision).where(DependencyDecision.collector_id == collector_id)
        ).all()
        decisions_by_key = {
            (
                decision.source_vm_uuid,
                decision.destination_identity,
                decision.protocol,
                decision.port_key,
                decision.category,
            ): decision
            for decision in decisions
        }
        for item in report["items"]:
            decision = decisions_by_key.get(
                (
                    str(item["source_vm_uuid"]),
                    str(item["destination_identity"]),
                    str(item["protocol"]).casefold(),
                    str(item["port_key"]),
                    str(item["category"]),
                )
            )
            item["planner_decision"] = decision.decision if decision else None
            item["planner_note"] = decision.note if decision else None
        return report

    @app.put(
        "/api/v1/dashboard/collectors/{collector_id}/dependency-decisions",
        dependencies=[Depends(require_dashboard)],
    )
    def save_dependency_decision(
        collector_id: str,
        payload: DependencyDecisionPayload,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        allowed_decisions = {
            "Confirmed hard dependency",
            "Soft dependency",
            "Confirmed shared service",
            "External dependency accepted",
            "Not relevant / excluded",
            "Requires action before cutover",
            "Network/firewall action required",
            "DNS/routing action required",
            "Owner validation required",
            "Target service setup required",
            "Investigate before cutover",
            "Requires hybrid plan",
        }
        if payload.decision not in allowed_decisions:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid decision",
            )
        if payload.decision in {"Confirmed hard dependency", "Not relevant / excluded"} and not (
            payload.note and payload.note.strip()
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="a planner note is required for this decision",
            )
        matching_items = dependency_review_report(session, collector_id)["items"]
        exists = any(
            item["source_vm_uuid"] == payload.source_vm_uuid
            and item["destination_identity"] == payload.destination_identity
            and str(item["protocol"]).casefold() == payload.protocol.casefold()
            and item["port_key"] == payload.port_key
            and item["category"] == payload.category
            for item in matching_items
        )
        if not exists:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="unknown dependency")
        decision = session.scalar(
            select(DependencyDecision).where(
                DependencyDecision.collector_id == collector_id,
                DependencyDecision.source_vm_uuid == payload.source_vm_uuid,
                DependencyDecision.destination_identity == payload.destination_identity,
                DependencyDecision.protocol == payload.protocol.casefold(),
                DependencyDecision.port_key == payload.port_key,
                DependencyDecision.category == payload.category,
            )
        )
        if decision is None:
            decision = DependencyDecision(id=str(uuid4()), collector_id=collector_id)
            session.add(decision)
        decision.source_vm_uuid = payload.source_vm_uuid
        decision.destination_identity = payload.destination_identity
        decision.protocol = payload.protocol.casefold()
        decision.port_key = payload.port_key
        decision.category = payload.category
        decision.decision = payload.decision
        decision.note = payload.note.strip() if payload.note and payload.note.strip() else None
        decision.updated_at = _now()
        session.commit()
        return {"status": "saved", "decision": decision.decision}

    @app.put(
        "/api/v1/dashboard/collectors/{collector_id}/dependency-decisions/batch",
        dependencies=[Depends(require_dashboard)],
    )
    def save_dependency_decisions(
        collector_id: str,
        payload: DependencyDecisionBatchPayload,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        """Apply one reviewed decision and note to a bounded set of known dependencies."""

        _require_collector(collector_id, session)
        allowed_decisions = {
            "Confirmed hard dependency",
            "Soft dependency",
            "Confirmed shared service",
            "External dependency accepted",
            "Not relevant / excluded",
            "Requires action before cutover",
            "Network/firewall action required",
            "DNS/routing action required",
            "Owner validation required",
            "Target service setup required",
            "Investigate before cutover",
            "Requires hybrid plan",
        }
        if payload.decision not in allowed_decisions:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid decision")
        note = payload.note.strip() if payload.note and payload.note.strip() else None
        if payload.decision in {"Confirmed hard dependency", "Not relevant / excluded"} and not note:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="a planner note is required for this decision",
            )

        dependency_keys = [
            (
                dependency.source_vm_uuid,
                dependency.destination_identity,
                dependency.protocol.casefold(),
                dependency.port_key,
                dependency.category,
            )
            for dependency in payload.dependencies
        ]
        if len(set(dependency_keys)) != len(dependency_keys):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="duplicate dependency selection",
            )
        available_keys = {
            (
                str(item["source_vm_uuid"]),
                str(item["destination_identity"]),
                str(item["protocol"]).casefold(),
                str(item["port_key"]),
                str(item["category"]),
            )
            for item in dependency_review_report(session, collector_id)["items"]
        }
        if not set(dependency_keys).issubset(available_keys):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="unknown dependency",
            )

        existing_decisions = session.scalars(
            select(DependencyDecision).where(DependencyDecision.collector_id == collector_id)
        ).all()
        decisions_by_key = {
            (
                decision.source_vm_uuid,
                decision.destination_identity,
                decision.protocol,
                decision.port_key,
                decision.category,
            ): decision
            for decision in existing_decisions
        }
        for source_vm_uuid, destination_identity, protocol, port_key, category in dependency_keys:
            decision = decisions_by_key.get(
                (source_vm_uuid, destination_identity, protocol, port_key, category)
            )
            if decision is None:
                decision = DependencyDecision(id=str(uuid4()), collector_id=collector_id)
                session.add(decision)
            decision.source_vm_uuid = source_vm_uuid
            decision.destination_identity = destination_identity
            decision.protocol = protocol
            decision.port_key = port_key
            decision.category = category
            decision.decision = payload.decision
            decision.note = note
            decision.updated_at = _now()
        session.commit()
        return {
            "status": "saved",
            "count": len(dependency_keys),
            "decision": payload.decision,
        }

    def _plan_response(plan: MigrationPlan, assignments: list[MigrationPlanVm]) -> dict[str, object]:
        """Serialize plan data without exposing collector credentials or tokens."""

        return {
            "plan_id": plan.id,
            "collector_id": plan.collector_id,
            "version": plan.version,
            "status": plan.status,
            "planner_name": plan.planner_name,
            "note": plan.note,
            "created_at": plan.created_at,
            "updated_at": plan.updated_at,
            "approved_at": plan.approved_at,
            "assignments": [
                {
                    "vm_uuid": assignment.vm_uuid,
                    "vm_name": assignment.vm_name,
                    "recommended_wave_number": assignment.recommended_wave_number,
                    "wave_number": assignment.wave_number,
                    "disposition": assignment.disposition,
                    "note": assignment.note,
                }
                for assignment in assignments
            ],
        }

    def _get_plan(collector_id: str, plan_id: str, session: Session) -> MigrationPlan:
        plan = session.get(MigrationPlan, plan_id)
        if plan is None or plan.collector_id != collector_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="migration plan not found")
        return plan

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/migration-plans",
        dependencies=[Depends(require_dashboard)],
    )
    def list_migration_plans(
        collector_id: str,
        session: Session = Depends(get_session),
    ) -> list[dict[str, object]]:
        _require_collector(collector_id, session)
        plans = session.scalars(
            select(MigrationPlan)
            .where(MigrationPlan.collector_id == collector_id)
            .order_by(MigrationPlan.version.desc())
        ).all()
        return [
            {
                "plan_id": plan.id,
                "version": plan.version,
                "status": plan.status,
                "planner_name": plan.planner_name,
                "note": plan.note,
                "created_at": plan.created_at,
                "updated_at": plan.updated_at,
                "approved_at": plan.approved_at,
            }
            for plan in plans
        ]

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/migration-plans/{plan_id}",
        dependencies=[Depends(require_dashboard)],
    )
    def get_migration_plan(
        collector_id: str,
        plan_id: str,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        plan = _get_plan(collector_id, plan_id, session)
        assignments = session.scalars(
            select(MigrationPlanVm)
            .where(MigrationPlanVm.plan_id == plan.id)
            .order_by(MigrationPlanVm.disposition, MigrationPlanVm.wave_number, MigrationPlanVm.vm_name)
        ).all()
        return _plan_response(plan, assignments)

    @app.post(
        "/api/v1/dashboard/collectors/{collector_id}/migration-plans",
        dependencies=[Depends(require_dashboard)],
        status_code=status.HTTP_201_CREATED,
    )
    def create_migration_plan(
        collector_id: str,
        payload: MigrationPlanCreatePayload,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        if session.scalar(
            select(MigrationPlan.id).where(
                MigrationPlan.collector_id == collector_id, MigrationPlan.status == "draft"
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="finish or approve the existing draft before creating another plan version",
            )
        recommended_waves = migration_waves(
            session, collector_id, observed_after, observed_before, use_approved_plan=False
        )
        if not recommended_waves:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="no eligible powered-on VMs are available for a migration plan",
            )
        current_version = session.scalar(
            select(func.max(MigrationPlan.version)).where(MigrationPlan.collector_id == collector_id)
        )
        now = _now()
        plan = MigrationPlan(
            id=str(uuid4()),
            collector_id=collector_id,
            version=(current_version or 0) + 1,
            status="draft",
            planner_name=payload.planner_name.strip(),
            note=payload.note.strip() if payload.note and payload.note.strip() else None,
            created_at=now,
            updated_at=now,
        )
        assignments: list[MigrationPlanVm] = []
        for wave in recommended_waves:
            for vm_uuid, vm_name in zip(
                wave["server_vm_uuids"], wave["server_names"], strict=True
            ):
                assignments.append(
                    MigrationPlanVm(
                        id=str(uuid4()),
                        plan_id=plan.id,
                        vm_uuid=str(vm_uuid),
                        vm_name=str(vm_name),
                        recommended_wave_number=int(wave["wave"]),
                        wave_number=int(wave["wave"]),
                        disposition="included",
                    )
                )
        session.add(plan)
        session.add_all(assignments)
        session.commit()
        return _plan_response(plan, assignments)

    @app.post(
        "/api/v1/dashboard/collectors/{collector_id}/migration-plans/{plan_id}/versions",
        dependencies=[Depends(require_dashboard)],
        status_code=status.HTTP_201_CREATED,
    )
    def clone_migration_plan(
        collector_id: str,
        plan_id: str,
        payload: MigrationPlanCreatePayload,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        source = _get_plan(collector_id, plan_id, session)
        if source.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="only an approved plan can be used as the baseline for a new version",
            )
        if session.scalar(
            select(MigrationPlan.id).where(
                MigrationPlan.collector_id == collector_id, MigrationPlan.status == "draft"
            )
        ):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a draft plan already exists")
        source_assignments = session.scalars(
            select(MigrationPlanVm).where(MigrationPlanVm.plan_id == source.id)
        ).all()
        current_version = session.scalar(
            select(func.max(MigrationPlan.version)).where(MigrationPlan.collector_id == collector_id)
        )
        now = _now()
        plan = MigrationPlan(
            id=str(uuid4()), collector_id=collector_id, version=(current_version or 0) + 1,
            status="draft", planner_name=payload.planner_name.strip(),
            note=payload.note.strip() if payload.note and payload.note.strip() else None,
            created_at=now, updated_at=now,
        )
        assignments = [
            MigrationPlanVm(
                id=str(uuid4()), plan_id=plan.id, vm_uuid=item.vm_uuid, vm_name=item.vm_name,
                recommended_wave_number=item.recommended_wave_number,
                wave_number=item.wave_number, disposition=item.disposition, note=item.note,
            )
            for item in source_assignments
        ]
        session.add(plan)
        session.add_all(assignments)
        session.commit()
        return _plan_response(plan, assignments)

    @app.put(
        "/api/v1/dashboard/collectors/{collector_id}/migration-plans/{plan_id}/assignments",
        dependencies=[Depends(require_dashboard)],
    )
    def update_migration_plan_assignment(
        collector_id: str,
        plan_id: str,
        payload: MigrationPlanAssignmentPayload,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        plan = _get_plan(collector_id, plan_id, session)
        if plan.status != "draft":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="approved plans are read-only")
        if payload.disposition not in {"included", "excluded"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid disposition")
        if payload.disposition == "included" and payload.wave_number is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="included VMs need a wave number")
        assignment = session.scalar(
            select(MigrationPlanVm).where(
                MigrationPlanVm.plan_id == plan.id, MigrationPlanVm.vm_uuid == payload.vm_uuid
            )
        )
        if assignment is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="VM is not in this plan")
        assignment.disposition = payload.disposition
        assignment.wave_number = payload.wave_number if payload.disposition == "included" else None
        assignment.note = payload.note.strip() if payload.note and payload.note.strip() else None
        plan.updated_at = _now()
        session.commit()
        return {"status": "saved", "plan_id": plan.id, "vm_uuid": assignment.vm_uuid}

    @app.put(
        "/api/v1/dashboard/collectors/{collector_id}/migration-plans/{plan_id}/bulk-assignments",
        dependencies=[Depends(require_dashboard)],
    )
    def update_migration_plan_assignments(
        collector_id: str,
        plan_id: str,
        payload: MigrationPlanBulkAssignmentPayload,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        plan = _get_plan(collector_id, plan_id, session)
        if plan.status != "draft":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="approved plans are read-only")
        if payload.disposition not in {"included", "excluded"}:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid disposition")
        if payload.disposition == "included" and payload.wave_number is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="included VMs need a wave number")
        unique_vm_uuids = set(payload.vm_uuids)
        if len(unique_vm_uuids) != len(payload.vm_uuids):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="duplicate VM selection")
        assignments = session.scalars(
            select(MigrationPlanVm).where(
                MigrationPlanVm.plan_id == plan.id, MigrationPlanVm.vm_uuid.in_(unique_vm_uuids)
            )
        ).all()
        if len(assignments) != len(unique_vm_uuids):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="one or more VMs are not in this plan")
        for assignment in assignments:
            assignment.disposition = payload.disposition
            assignment.wave_number = payload.wave_number if payload.disposition == "included" else None
            assignment.note = payload.note.strip() if payload.note and payload.note.strip() else None
        plan.updated_at = _now()
        session.commit()
        return {"status": "saved", "plan_id": plan.id, "vm_count": len(assignments)}

    @app.post(
        "/api/v1/dashboard/collectors/{collector_id}/migration-plans/{plan_id}/approve",
        dependencies=[Depends(require_dashboard)],
    )
    def approve_migration_plan(
        collector_id: str,
        plan_id: str,
        payload: MigrationPlanApprovalPayload,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _require_collector(collector_id, session)
        plan = _get_plan(collector_id, plan_id, session)
        if plan.status != "draft":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="only a draft plan can be approved")
        included_count = session.scalar(
            select(func.count(MigrationPlanVm.id)).where(
                MigrationPlanVm.plan_id == plan.id, MigrationPlanVm.disposition == "included"
            )
        )
        if not included_count:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="a plan needs at least one included VM")
        now = _now()
        session.execute(
            update(MigrationPlan)
            .where(MigrationPlan.collector_id == collector_id, MigrationPlan.status == "approved")
            .values(status="superseded", updated_at=now)
        )
        plan.status = "approved"
        plan.planner_name = payload.planner_name.strip()
        plan.note = payload.note.strip() if payload.note and payload.note.strip() else plan.note
        plan.approved_at = now
        plan.updated_at = now
        included_vm_uuids = {
            assignment.vm_uuid
            for assignment in session.scalars(
                select(MigrationPlanVm).where(
                    MigrationPlanVm.plan_id == plan.id,
                    MigrationPlanVm.disposition == "included",
                )
            ).all()
        }
        baseline_dependencies = plan_dependency_report(session, collector_id)
        session.add_all(
            MigrationPlanDependency(
                id=str(uuid4()),
                plan_id=plan.id,
                source_vm_uuid=str(item["source_vm_uuid"]),
                destination_identity=str(item["destination_identity"]),
                protocol=str(item["protocol"]).casefold(),
                port_key=str(item["port_key"]),
                category=str(item["category"]),
            )
            for item in baseline_dependencies
            if str(item["source_vm_uuid"]) in included_vm_uuids
            or str(item.get("destination_vm_uuid") or "") in included_vm_uuids
        )
        session.commit()
        return {"status": "approved", "plan_id": plan.id, "version": plan.version}

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
        use_approved_plan: bool = True,
    ) -> list[dict[str, object]]:
        _require_collector(collector_id, session)
        return migration_waves(
            session,
            collector_id,
            observed_after,
            observed_before,
            use_approved_plan=use_approved_plan,
        )

    @app.get(
        "/api/v1/dashboard/collectors/{collector_id}/wave-summary",
        dependencies=[Depends(require_dashboard)],
    )
    def collector_wave_summary(
        collector_id: str,
        session: Session = Depends(get_session),
        observed_after: datetime | None = None,
        observed_before: datetime | None = None,
        use_approved_plan: bool = True,
    ) -> dict[str, int]:
        _require_collector(collector_id, session)
        return wave_summary(
            session,
            collector_id,
            observed_after,
            observed_before,
            use_approved_plan=use_approved_plan,
        )

    return app
