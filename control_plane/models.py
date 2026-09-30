"""Persistent, non-secret control-plane identities and telemetry."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from control_plane.db import Base


class Enrollment(Base):
    """A short-lived, one-use collector enrollment code digest."""

    __tablename__ = "enrollments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    code_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReconnectionCode(Base):
    """A short-lived, one-use authorization to rotate one collector's agent token."""

    __tablename__ = "reconnection_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    code_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Collector(Base):
    """A registered customer-side agent. No vCenter or guest secrets are stored."""

    __tablename__ = "collectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    agent_token_digest: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    software_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="online", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ObservationBatch(Base):
    """Idempotency marker for a collector telemetry upload."""

    __tablename__ = "observation_batches"
    __table_args__ = (UniqueConstraint("collector_id", "sequence", name="uq_batch_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Observation(Base):
    """Normalized dependency telemetry without credentials or packet payloads."""

    __tablename__ = "observations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    source_vm_uuid: Mapped[str] = mapped_column(String(64), index=True)
    source_ip: Mapped[str] = mapped_column(String(45), index=True)
    destination_ip: Mapped[str] = mapped_column(String(45), index=True)
    destination_port: Mapped[int] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(String(8))
    collector_type: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    process: Mapped[str | None] = mapped_column(Text, nullable=True)


class InventoryBatch(Base):
    """Idempotency marker for a complete inventory snapshot upload."""

    __tablename__ = "inventory_batches"
    __table_args__ = (
        UniqueConstraint("collector_id", "sequence", name="uq_inventory_batch_sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class InventoryVm(Base):
    """Most recent VM inventory state for one registered collector."""

    __tablename__ = "inventory_vms"
    __table_args__ = (UniqueConstraint("collector_id", "vm_uuid", name="uq_collector_vm"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    vm_uuid: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ips: Mapped[str] = mapped_column(Text, default="")
    cluster: Mapped[str | None] = mapped_column(String(255), nullable=True)
    folder: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    power_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class InventoryVmState(Base):
    """Append-only inventory state evidence used to explain wave eligibility."""

    __tablename__ = "inventory_vm_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    vm_uuid: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255))
    ips: Mapped[str] = mapped_column(Text, default="")
    power_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class DependencyDecision(Base):
    """Planner decision for one normalized dependency-review relationship."""

    __tablename__ = "dependency_decisions"
    __table_args__ = (
        UniqueConstraint(
            "collector_id",
            "source_vm_uuid",
            "destination_identity",
            "protocol",
            "port_key",
            "category",
            name="uq_dependency_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    source_vm_uuid: Mapped[str] = mapped_column(String(64), index=True)
    destination_identity: Mapped[str] = mapped_column(String(64), index=True)
    protocol: Mapped[str] = mapped_column(String(8))
    port_key: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class MigrationPlan(Base):
    """A versioned, planner-owned migration plan for one collector."""

    __tablename__ = "migration_plans"
    __table_args__ = (UniqueConstraint("collector_id", "version", name="uq_migration_plan_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collector_id: Mapped[str] = mapped_column(ForeignKey("collectors.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    planner_name: Mapped[str] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MigrationPlanVm(Base):
    """One VM assignment frozen into a versioned migration plan."""

    __tablename__ = "migration_plan_vms"
    __table_args__ = (UniqueConstraint("plan_id", "vm_uuid", name="uq_migration_plan_vm"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("migration_plans.id"), index=True)
    vm_uuid: Mapped[str] = mapped_column(String(64), index=True)
    vm_name: Mapped[str] = mapped_column(String(255))
    recommended_wave_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wave_number: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    disposition: Mapped[str] = mapped_column(String(16), default="included", index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class MigrationPlanDependency(Base):
    """Normalized dependency evidence frozen when a migration plan is approved."""

    __tablename__ = "migration_plan_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "plan_id",
            "source_vm_uuid",
            "destination_identity",
            "protocol",
            "port_key",
            "category",
            name="uq_migration_plan_dependency",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("migration_plans.id"), index=True)
    source_vm_uuid: Mapped[str] = mapped_column(String(64), index=True)
    destination_identity: Mapped[str] = mapped_column(String(64), index=True)
    protocol: Mapped[str] = mapped_column(String(8))
    port_key: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(64))
