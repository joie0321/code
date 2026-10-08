"""Collector-scoped connection reporting and migration-wave analysis."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from control_plane.models import (
    Collector,
    DependencyDecision,
    InventoryVm,
    MigrationPlan,
    MigrationPlanDependency,
    MigrationPlanVm,
    Observation,
)

_BACKGROUND_UDP_SERVICES = {
    53: "DNS",
    67: "DHCP",
    68: "DHCP",
    123: "NTP",
    546: "DHCPv6",
    547: "DHCPv6",
}
_CANDIDATE_SHARED_TCP_SERVICES = {
    53: "DNS",
    88: "Kerberos",
    389: "LDAP",
    636: "LDAPS",
    3268: "Global Catalog",
    3269: "Global Catalog over TLS",
}
_DYNAMIC_PRIVATE_PORT_START = 32768
_DYNAMIC_PRIVATE_PORT_END = 65535
_READINESS_STATUS_BY_DECISION = {
    "Not relevant / excluded": "Resolved",
    "External dependency accepted": "Resolved",
    "Confirmed shared service": "Resolved",
    "Soft dependency": "Advisory",
    "Network/firewall action required": "Action required",
    "DNS/routing action required": "Action required",
    "Target service setup required": "Action required",
    "Owner validation required": "Investigate",
    "Investigate before cutover": "Investigate",
    "Confirmed hard dependency": "High impact",
    "Requires hybrid plan": "High impact",
}


def dependency_readiness_status(decision: str | None, category: str) -> str:
    """Map a planner decision to the one status used by readiness views."""

    if not decision and category == "Unavailable internal VM":
        return "High impact"
    return _READINESS_STATUS_BY_DECISION.get(decision or "", "Investigate")


def _traffic_class(protocol: str, destination_port: int) -> str:
    """Classify UDP safely without allowing it to influence application waves."""

    if protocol == "udp":
        service = _BACKGROUND_UDP_SERVICES.get(destination_port)
        return f"Background infrastructure ({service})" if service else "UDP reporting only"
    return "Application TCP"


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _active_inventory(session: Session, collector_id: str) -> list[InventoryVm]:
    return session.scalars(
        select(InventoryVm).where(
            InventoryVm.collector_id == collector_id,
            InventoryVm.is_active.is_(True),
        )
    ).all()


def _inventory(session: Session, collector_id: str) -> list[InventoryVm]:
    return session.scalars(
        select(InventoryVm).where(InventoryVm.collector_id == collector_id)
    ).all()


def _eligible_vms(vms: list[InventoryVm]) -> list[InventoryVm]:
    return [
        vm
        for vm in vms
        if vm.is_active and (vm.power_state or "").casefold() == "poweredon"
    ]


def _unavailable_reason(vm: InventoryVm) -> str:
    if not vm.is_active:
        return "VM is not present in the latest inventory snapshot."
    state = vm.power_state or "unknown"
    return f"VM power state is {state}; only poweredOn VMs are eligible for a current wave."


def _vms_by_ip(vms: list[InventoryVm]) -> dict[str, InventoryVm]:
    """Prefer current inventory assignments over retained inactive VM addresses."""

    by_ip = {ip: vm for vm in vms if not vm.is_active for ip in vm.ips.split(",") if ip}
    by_ip.update({ip: vm for vm in vms if vm.is_active for ip in vm.ips.split(",") if ip})
    return by_ip


def _observations(
    session: Session,
    collector_id: str,
    observed_after: datetime | None,
    observed_before: datetime | None,
):
    query = select(Observation).where(Observation.collector_id == collector_id)
    if observed_after:
        query = query.where(Observation.observed_at >= _utc_naive(observed_after))
    if observed_before:
        query = query.where(Observation.observed_at <= _utc_naive(observed_before))
    return session.scalars(query.order_by(Observation.observed_at.desc())).all()


def connection_report(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, object]]:
    """Return bounded, collector-local observations without external DNS lookups."""

    inventory = _inventory(session, collector_id)
    vms_by_uuid = {vm.vm_uuid: vm for vm in inventory}
    vms_by_ip = _vms_by_ip(inventory)
    report: list[dict[str, object]] = []
    observations = _observations(session, collector_id, observed_after, observed_before)
    for observation in observations[:limit]:
        source = vms_by_uuid.get(observation.source_vm_uuid)
        if source is None:
            continue
        destination = vms_by_ip.get(observation.destination_ip)
        report.append(
            {
                "source_vm_uuid": source.vm_uuid,
                "source_vm_name": source.name,
                "destination_ip": observation.destination_ip,
                "destination_vm_uuid": destination.vm_uuid if destination else None,
                "destination_vm_name": destination.name if destination else None,
                "destination_hostname": (destination.hostname or destination.name)
                if destination
                else None,
                "destination_power_state": destination.power_state if destination else None,
                "destination_port": observation.destination_port,
                "protocol": observation.protocol,
                "traffic_class": _traffic_class(observation.protocol, observation.destination_port),
                "collector_type": observation.collector_type,
                "process": observation.process,
                "observed_at": observation.observed_at.isoformat(),
            }
        )
    return report


def wave_connection_report(
    session: Session,
    collector_id: str,
    vm_uuids: list[str],
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
    page: int = 1,
    page_size: int = 100,
    deduplicate: bool = False,
    exclude_dynamic_private_ports: bool = False,
) -> dict[str, object]:
    """Return one bounded page of evidence for the selected wave VMs.

    Filtering happens in the database before pagination.  This prevents a busy
    collector's newest global observations from hiding older evidence for a
    smaller selected wave. When requested, repeated observations of one logical
    connection are collapsed before pagination.
    """

    selected_vm_uuids = set(vm_uuids)
    inventory = _inventory(session, collector_id)
    selected_vms = [vm for vm in inventory if vm.vm_uuid in selected_vm_uuids]
    selected_vm_uuids = {vm.vm_uuid for vm in selected_vms}
    selected_ips = {ip for vm in selected_vms for ip in vm.ips.split(",") if ip}
    if not selected_vm_uuids:
        return {"items": [], "page": 1, "page_size": page_size, "total": 0}

    filters = [Observation.collector_id == collector_id]
    if observed_after:
        filters.append(Observation.observed_at >= _utc_naive(observed_after))
    if observed_before:
        filters.append(Observation.observed_at <= _utc_naive(observed_before))
    endpoint_filters = [Observation.source_vm_uuid.in_(selected_vm_uuids)]
    if selected_ips:
        endpoint_filters.append(Observation.destination_ip.in_(selected_ips))
    filters.append(or_(*endpoint_filters))
    if exclude_dynamic_private_ports:
        filters.append(
            or_(
                Observation.destination_port < _DYNAMIC_PRIVATE_PORT_START,
                Observation.destination_port > _DYNAMIC_PRIVATE_PORT_END,
            )
        )

    all_vms_by_uuid = {vm.vm_uuid: vm for vm in inventory}
    all_vms_by_ip = _vms_by_ip(inventory)
    if deduplicate:
        grouped = (
            select(
                Observation.source_vm_uuid,
                Observation.destination_ip,
                Observation.destination_port,
                Observation.protocol,
                func.count().label("observation_count"),
                func.min(Observation.observed_at).label("first_observed"),
                func.max(Observation.observed_at).label("last_observed"),
            )
            .where(*filters)
            .group_by(
                Observation.source_vm_uuid,
                Observation.destination_ip,
                Observation.destination_port,
                Observation.protocol,
            )
            .order_by(
                Observation.source_vm_uuid,
                Observation.destination_ip,
                Observation.protocol,
                Observation.destination_port,
            )
        )
        total = session.scalar(select(func.count()).select_from(grouped.subquery())) or 0
        total_pages = max(1, (total + page_size - 1) // page_size)
        current_page = min(page, total_pages)
        rows = session.execute(
            grouped.offset((current_page - 1) * page_size).limit(page_size)
        ).all()
        items: list[dict[str, object]] = []
        for (
            source_vm_uuid,
            destination_ip,
            destination_port,
            protocol,
            observation_count,
            first_observed,
            last_observed,
        ) in rows:
            source = all_vms_by_uuid.get(source_vm_uuid)
            if source is None:
                continue
            destination = all_vms_by_ip.get(destination_ip)
            items.append(
                {
                    "source_vm_uuid": source.vm_uuid,
                    "source_vm_name": source.name,
                    "destination_ip": destination_ip,
                    "destination_vm_uuid": destination.vm_uuid if destination else None,
                    "destination_vm_name": destination.name if destination else None,
                    "destination_hostname": (destination.hostname or destination.name)
                    if destination
                    else None,
                    "destination_power_state": destination.power_state if destination else None,
                    "destination_port": destination_port,
                    "protocol": protocol,
                    "traffic_class": _traffic_class(protocol, destination_port),
                    "evidence": (
                        "Observed once" if observation_count == 1 else "Observed repeatedly"
                    ),
                    "observation_count": observation_count,
                    "first_observed": first_observed.isoformat(),
                    "last_observed": last_observed.isoformat(),
                }
            )
        return {"items": items, "page": current_page, "page_size": page_size, "total": total}

    total = session.scalar(select(func.count()).select_from(Observation).where(*filters)) or 0
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    observations = session.scalars(
        select(Observation)
        .where(*filters)
        .order_by(Observation.observed_at.desc())
        .offset((current_page - 1) * page_size)
        .limit(page_size)
    ).all()
    items: list[dict[str, object]] = []
    for observation in observations:
        source = all_vms_by_uuid.get(observation.source_vm_uuid)
        if source is None:
            continue
        destination = all_vms_by_ip.get(observation.destination_ip)
        items.append(
            {
                "source_vm_uuid": source.vm_uuid,
                "source_vm_name": source.name,
                "destination_ip": observation.destination_ip,
                "destination_vm_uuid": destination.vm_uuid if destination else None,
                "destination_vm_name": destination.name if destination else None,
                "destination_hostname": (destination.hostname or destination.name)
                if destination
                else None,
                "destination_power_state": destination.power_state if destination else None,
                "destination_port": observation.destination_port,
                "protocol": observation.protocol,
                "traffic_class": _traffic_class(observation.protocol, observation.destination_port),
                "collector_type": observation.collector_type,
                "process": observation.process,
                "observed_at": observation.observed_at.isoformat(),
            }
        )
    return {"items": items, "page": current_page, "page_size": page_size, "total": total}


def detailed_connection_report(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
) -> list[dict[str, object]]:
    """Aggregate connection evidence for the detailed engineer-facing export."""

    inventory = _inventory(session, collector_id)
    vms_by_uuid = {vm.vm_uuid: vm for vm in inventory}
    vms_by_ip = _vms_by_ip(inventory)
    aggregated: dict[tuple[str, str, str], dict[str, object]] = {}
    for observation in _observations(session, collector_id, observed_after, observed_before):
        source = vms_by_uuid.get(observation.source_vm_uuid)
        if source is None:
            continue
        destination = vms_by_ip.get(observation.destination_ip)
        key = (source.vm_uuid, observation.destination_ip, observation.protocol)
        row = aggregated.setdefault(
            key,
            {
                "source": source.name,
                "source_ips": set(),
                "destination": (destination.hostname or destination.name)
                if destination
                else "External",
                "destination_ip": observation.destination_ip,
                "protocol": observation.protocol.upper(),
                "destination_ports": set(),
            },
        )
        row["source_ips"].add(observation.source_ip)  # type: ignore[union-attr]
        row["destination_ports"].add(observation.destination_port)  # type: ignore[union-attr]

    rows = [
        {
            "source": row["source"],
            "source_ips": ", ".join(sorted(row["source_ips"])),
            "destination": row["destination"],
            "destination_ip": row["destination_ip"],
            "protocol": row["protocol"],
            "destination_ports": ", ".join(str(port) for port in sorted(row["destination_ports"])),
        }
        for row in aggregated.values()
    ]
    return sorted(
        rows,
        key=lambda row: (
            str(row["source"]),
            str(row["destination"]),
            str(row["protocol"]),
        ),
    )


def dependency_review_report(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
) -> dict[str, object]:
    """Aggregate non-grouping dependencies that require migration review."""

    inventory = _inventory(session, collector_id)
    by_uuid = {vm.vm_uuid: vm for vm in inventory}
    by_ip = _vms_by_ip(inventory)
    rows: dict[tuple[str, str, str, str, int], dict[str, object]] = {}
    for observation in _observations(session, collector_id, observed_after, observed_before):
        source = by_uuid.get(observation.source_vm_uuid)
        if source is None:
            continue
        destination = by_ip.get(observation.destination_ip)
        category: str | None = None
        classification: str | None = None
        reason: str | None = None
        if not (
            source.is_active
            and (destination is None or destination.is_active)
            and (source.power_state or "").casefold() == "poweredon"
            and (
                destination is None
                or (destination.power_state or "").casefold() == "poweredon"
            )
        ):
            category, classification = "Unavailable internal VM", "Unavailable internal"
            reason = "An internal endpoint is unavailable in the latest inventory."
        elif observation.protocol == "udp" and observation.destination_port in _BACKGROUND_UDP_SERVICES:
            service = _BACKGROUND_UDP_SERVICES[observation.destination_port]
            category, classification = "Shared infrastructure", service
            reason = f"{service} traffic is reporting-only and never merges migration waves."
        elif observation.protocol == "tcp" and observation.destination_port in _CANDIDATE_SHARED_TCP_SERVICES:
            service = _CANDIDATE_SHARED_TCP_SERVICES[observation.destination_port]
            category, classification = "Shared infrastructure candidate", service
            reason = f"TCP/{observation.destination_port} can be {service}; confirm with the owner."
        elif destination is None:
            category, classification = "External dependency", "External endpoint"
            reason = "External endpoints never merge migration waves."
        if category is None or classification is None or reason is None:
            continue
        dynamic_private_port = (
            observation.protocol == "tcp"
            and _DYNAMIC_PRIVATE_PORT_START <= observation.destination_port <= _DYNAMIC_PRIVATE_PORT_END
        )
        port_key: str | int = (
            "dynamic-private" if dynamic_private_port else observation.destination_port
        )
        port_service = (
            "Dynamic/private TCP ports"
            if dynamic_private_port
            else f"{classification} (TCP/{observation.destination_port})"
            if category.startswith("Shared infrastructure")
            else f"{observation.protocol.upper()}/{observation.destination_port}"
        )
        destination_key = destination.vm_uuid if destination else observation.destination_ip
        key = (
            category,
            source.vm_uuid,
            destination_key,
            observation.protocol,
            port_key,
        )
        row = rows.setdefault(
            key,
            {
                "category": category,
                "classification": classification,
                "reason": reason,
                "source_vm_uuid": source.vm_uuid,
                "source_vm": source.name,
                "destination_vm_uuid": destination.vm_uuid if destination else None,
                "destination_identity": destination_key,
                "port_key": str(port_key),
                "unavailable_vm_uuid": (
                    source.vm_uuid
                    if not source.is_active or (source.power_state or "").casefold() != "poweredon"
                    else destination.vm_uuid
                    if destination
                    else None
                ),
                "destination": (destination.hostname or destination.name)
                if destination
                else "External",
                "destination_ip": observation.destination_ip,
                "protocol": observation.protocol.upper(),
                "port_service": port_service,
                "observation_count": 0,
            },
        )
        row["observation_count"] += 1
    items = list(rows.values())
    items.sort(key=lambda row: (str(row["category"]), -int(row["observation_count"])))
    return {
        "items": items,
        "summary": {
            category: sum(1 for row in items if row["category"] == category)
            for category in (
                "External dependency",
                "Shared infrastructure",
                "Shared infrastructure candidate",
                "Unavailable internal VM",
            )
        },
    }


def plan_dependency_report(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
) -> list[dict[str, object]]:
    """Normalize all plan-relevant relationships, including internal TCP VM edges."""

    items = list(
        dependency_review_report(session, collector_id, observed_after, observed_before)["items"]
    )
    inventory = _inventory(session, collector_id)
    by_uuid = {vm.vm_uuid: vm for vm in inventory}
    by_ip = _vms_by_ip(inventory)
    internal_rows: dict[tuple[str, str, str, str], dict[str, object]] = {}
    for observation in _observations(session, collector_id, observed_after, observed_before):
        source = by_uuid.get(observation.source_vm_uuid)
        destination = by_ip.get(observation.destination_ip)
        if source is None or destination is None or source.vm_uuid == destination.vm_uuid:
            continue
        port_key = (
            "dynamic-private"
            if observation.protocol == "tcp"
            and _DYNAMIC_PRIVATE_PORT_START <= observation.destination_port <= _DYNAMIC_PRIVATE_PORT_END
            else str(observation.destination_port)
        )
        key = (source.vm_uuid, destination.vm_uuid, observation.protocol, str(port_key))
        internal_rows.setdefault(
            key,
            {
                "category": "Internal VM dependency",
                "source_vm_uuid": source.vm_uuid,
                "source_vm": source.name,
                "destination_vm_uuid": destination.vm_uuid,
                "destination_identity": destination.vm_uuid,
                "destination": destination.hostname or destination.name,
                "destination_ip": observation.destination_ip,
                "protocol": observation.protocol.upper(),
                "port_key": str(port_key),
                "port_service": (
                    "Dynamic/private TCP ports"
                    if port_key == "dynamic-private"
                    else f"{observation.protocol.upper()}/{observation.destination_port}"
                ),
            },
        )
    return items + list(internal_rows.values())


def migration_waves(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
    use_approved_plan: bool = True,
) -> list[dict[str, object]]:
    """Build waves from connected active, powered-on VMs in one collector inventory."""

    if use_approved_plan:
        approved_plan = session.scalar(
            select(MigrationPlan)
            .where(
                MigrationPlan.collector_id == collector_id,
                MigrationPlan.status == "approved",
            )
            .order_by(MigrationPlan.approved_at.desc(), MigrationPlan.version.desc())
        )
        if approved_plan is not None:
            assignments = session.scalars(
                select(MigrationPlanVm)
                .where(
                    MigrationPlanVm.plan_id == approved_plan.id,
                    MigrationPlanVm.disposition == "included",
                    MigrationPlanVm.wave_number.is_not(None),
                )
                .order_by(MigrationPlanVm.wave_number, MigrationPlanVm.vm_name)
            ).all()
            by_wave: dict[int, list[MigrationPlanVm]] = defaultdict(list)
            for assignment in assignments:
                if assignment.wave_number is not None:
                    by_wave[assignment.wave_number].append(assignment)
            return [
                {
                    "wave": wave_number,
                    "server_vm_uuids": [assignment.vm_uuid for assignment in members],
                    "server_names": [assignment.vm_name for assignment in members],
                    "inactive_internal_dependencies": [],
                    "plan_id": approved_plan.id,
                    "plan_version": approved_plan.version,
                    "plan_status": "approved",
                    "reason": "Planner-approved migration plan.",
                }
                for wave_number, members in sorted(by_wave.items())
            ]

    inventory = _inventory(session, collector_id)
    eligible = _eligible_vms(inventory)
    names_by_uuid = {vm.vm_uuid: vm.name for vm in eligible}
    inventory_by_uuid = {vm.vm_uuid: vm for vm in inventory}
    vm_uuid_by_ip = {ip: vm.vm_uuid for vm in eligible for ip in vm.ips.split(",") if ip}
    known_vm_by_ip = _vms_by_ip(inventory)
    graph: dict[str, set[str]] = defaultdict(set)
    unavailable_by_eligible_vm: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    for observation in _observations(session, collector_id, observed_after, observed_before):
        if observation.protocol != "tcp":
            continue
        source_uuid = observation.source_vm_uuid
        destination_uuid = vm_uuid_by_ip.get(observation.destination_ip)
        if source_uuid in names_by_uuid and destination_uuid and destination_uuid != source_uuid:
            graph[source_uuid].add(destination_uuid)
            graph[destination_uuid].add(source_uuid)
            continue

        source = inventory_by_uuid.get(source_uuid)
        destination = known_vm_by_ip.get(observation.destination_ip)
        endpoints = (
            (source_uuid, source),
            (destination.vm_uuid, destination) if destination else (None, None),
        )
        eligible_endpoints = [
            (vm_uuid, vm)
            for vm_uuid, vm in endpoints
            if vm_uuid in names_by_uuid and vm is not None
        ]
        unavailable_endpoints = [
            vm
            for vm_uuid, vm in endpoints
            if vm_uuid not in names_by_uuid and vm is not None
        ]
        for eligible_uuid, _ in eligible_endpoints:
            for unavailable in unavailable_endpoints:
                if unavailable.vm_uuid == eligible_uuid:
                    continue
                dependency = unavailable_by_eligible_vm[eligible_uuid].setdefault(
                    unavailable.vm_uuid,
                    {
                        "vm_uuid": unavailable.vm_uuid,
                        "name": unavailable.name,
                        "power_state": unavailable.power_state,
                        "is_active": unavailable.is_active,
                        "reason": _unavailable_reason(unavailable),
                        "connection_count": 0,
                    },
                )
                dependency["connection_count"] += 1

    components: list[set[str]] = []
    remaining = set(names_by_uuid)
    while remaining:
        seed = remaining.pop()
        component, todo = set(), [seed]
        while todo:
            current = todo.pop()
            if current in component:
                continue
            component.add(current)
            for neighbor in graph[current]:
                if neighbor not in component:
                    todo.append(neighbor)
            remaining.discard(current)
        components.append(component)
    components.sort(key=lambda component: (-len(component), sorted(component)))
    return [
        {
            "wave": index,
            "server_vm_uuids": sorted(component),
            "server_names": [names_by_uuid[vm_uuid] for vm_uuid in sorted(component)],
            "inactive_internal_dependencies": sorted(
                {
                    dependency["vm_uuid"]: dependency
                    for vm_uuid in component
                    for dependency in unavailable_by_eligible_vm.get(vm_uuid, {}).values()
                }.values(),
                key=lambda dependency: (str(dependency["name"]), str(dependency["vm_uuid"])),
            ),
            "reason": (
                "Observed TCP dependencies connect these servers; review external and shared "
                "service dependencies before cutover."
                if len(component) > 1
                else "No internal TCP dependency was observed in the selected time range."
            ),
            "plan_status": "recommended",
        }
        for index, component in enumerate(components, start=1)
    ]


def wave_summary(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
    use_approved_plan: bool = True,
) -> dict[str, int]:
    inventory = _active_inventory(session, collector_id)
    eligible = _eligible_vms(inventory)
    eligible_uuids = {vm.vm_uuid for vm in eligible}
    eligible_by_ip = {ip: vm.vm_uuid for vm in eligible for ip in vm.ips.split(",") if ip}
    connected: set[str] = set()
    for observation in _observations(session, collector_id, observed_after, observed_before):
        if observation.protocol != "tcp":
            continue
        if observation.source_vm_uuid in eligible_uuids:
            connected.add(observation.source_vm_uuid)
        destination_uuid = eligible_by_ip.get(observation.destination_ip)
        if destination_uuid:
            connected.add(destination_uuid)
    waves = migration_waves(
        session,
        collector_id,
        observed_after,
        observed_before,
        use_approved_plan=use_approved_plan,
    )
    # A migration wave has a connection only when it contains an internal
    # VM-to-VM TCP edge.  A VM may have TCP observations to an external IP,
    # but those observations are reporting context and must not make its
    # singleton wave appear to be an internal dependency wave.
    waves_with_connections = sum(len(wave["server_vm_uuids"]) > 1 for wave in waves)
    return {
        "wave_count": len(waves),
        "active_inventory_vm_count": len(inventory),
        "eligible_vm_count": len(eligible),
        "waves_with_observed_connections": waves_with_connections,
        "waves_without_observed_connections": len(waves) - waves_with_connections,
        "vms_without_observed_connections": len(eligible) - len(connected),
    }


def plan_drift_report(session: Session, collector_id: str) -> dict[str, object]:
    """Compare approved plan membership and later normalized dependencies without changing it."""

    plan = session.scalar(
        select(MigrationPlan)
        .where(MigrationPlan.collector_id == collector_id, MigrationPlan.status == "approved")
        .order_by(MigrationPlan.approved_at.desc())
    )
    if plan is None:
        return {"status": "no_approved_plan", "items": []}
    assignments = session.scalars(
        select(MigrationPlanVm).where(MigrationPlanVm.plan_id == plan.id)
    ).all()
    inventory = {vm.vm_uuid: vm for vm in _inventory(session, collector_id)}
    items: list[dict[str, object]] = []
    planned = {item.vm_uuid for item in assignments if item.disposition == "included"}
    excluded = {item.vm_uuid for item in assignments if item.disposition == "excluded"}
    for vm_uuid in planned:
        vm = inventory.get(vm_uuid)
        if vm is None:
            assignment = next(item for item in assignments if item.vm_uuid == vm_uuid)
            items.append({"severity": "Critical", "vm_name": assignment.vm_name, "reason": "Approved VM is missing from current inventory."})
        elif not vm.is_active or vm.power_state != "poweredOn":
            items.append({"severity": "Critical", "vm_name": vm.name, "reason": "Approved VM is not currently active and powered on."})
    for vm in _eligible_vms(list(inventory.values())):
        if vm.vm_uuid not in planned and vm.vm_uuid not in excluded:
            items.append({"severity": "Warning", "vm_name": vm.name, "reason": "Current powered-on VM is not in the approved plan."})
    baseline_rows = session.scalars(
        select(MigrationPlanDependency).where(MigrationPlanDependency.plan_id == plan.id)
    ).all()
    baseline_keys = {
        (
            row.source_vm_uuid,
            row.destination_identity,
            row.protocol,
            row.port_key,
            row.category,
        )
        for row in baseline_rows
    }
    if not baseline_keys and plan.approved_at is not None:
        # Plans approved before dependency baselines were introduced remain usable.
        baseline_keys = {
            (
                str(item["source_vm_uuid"]),
                str(item["destination_identity"]),
                str(item["protocol"]).casefold(),
                str(item["port_key"]),
                str(item["category"]),
            )
            for item in plan_dependency_report(
                session, collector_id, observed_before=plan.approved_at
            )
        }
    if plan.approved_at is not None:
        later_dependencies = plan_dependency_report(
            session, collector_id, observed_after=plan.approved_at
        )
        for dependency in later_dependencies:
            source_vm_uuid = str(dependency["source_vm_uuid"])
            destination_vm_uuid = str(dependency.get("destination_vm_uuid") or "")
            if source_vm_uuid not in planned and destination_vm_uuid not in planned:
                continue
            key = (
                source_vm_uuid,
                str(dependency["destination_identity"]),
                str(dependency["protocol"]).casefold(),
                str(dependency["port_key"]),
                str(dependency["category"]),
            )
            if key in baseline_keys:
                continue
            severity = (
                "Critical"
                if dependency["category"] == "Unavailable internal VM"
                or destination_vm_uuid in planned
                else "Warning"
            )
            items.append(
                {
                    "severity": severity,
                    "vm_name": dependency["source_vm"],
                    "reason": (
                        "New dependency observed after plan approval: "
                        f"{dependency['destination']} ({dependency['destination_ip']}) "
                        f"{dependency['port_service']}."
                    ),
                }
            )
    return {"status": "needs_review" if items else "current", "plan_version": plan.version, "items": items}


def wave_readiness_report(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
    use_approved_plan: bool = True,
) -> dict[str, object]:
    """Explain evidence confidence and outstanding risks for every candidate wave."""

    now = datetime.now(UTC)
    waves = migration_waves(
        session,
        collector_id,
        observed_after,
        observed_before,
        use_approved_plan=use_approved_plan,
    )
    inventory = _active_inventory(session, collector_id)
    observations = _observations(session, collector_id, observed_after, observed_before)
    dependencies = dependency_review_report(session, collector_id, observed_after, observed_before)["items"]
    decisions = session.scalars(
        select(DependencyDecision).where(DependencyDecision.collector_id == collector_id)
    ).all()
    decision_keys = {
        (d.source_vm_uuid, d.destination_identity, d.protocol, d.port_key, d.category): d
        for d in decisions
    }
    collector = session.get(Collector, collector_id)
    window_seconds = (
        (observed_before - observed_after).total_seconds()
        if observed_after and observed_before
        else 0
    )
    observed_span = (
        (max(item.observed_at for item in observations) - min(item.observed_at for item in observations)).total_seconds()
        if len(observations) > 1
        else 0
    )
    coverage = min(1.0, observed_span / window_seconds) if window_seconds else 1.0
    newest_inventory = max((vm.updated_at for vm in inventory), default=None)
    inventory_age_days = (now - newest_inventory.astimezone(UTC)).total_seconds() / 86400 if newest_inventory else 999
    collector_online = bool(collector and (now - collector.last_seen_at.astimezone(UTC)).total_seconds() <= 180)
    rows: list[dict[str, object]] = []
    for wave in waves:
        wave_vm_uuids = set(wave["server_vm_uuids"])
        wave_dependencies = [
            item
            for item in dependencies
            if item["source_vm_uuid"] in wave_vm_uuids
            or item.get("destination_vm_uuid") in wave_vm_uuids
        ]
        review_items = []
        review_counts = {
            "Resolved": 0,
            "Advisory": 0,
            "Action required": 0,
            "Investigate": 0,
            "High impact": 0,
        }
        for item in wave_dependencies:
            key = (
                str(item["source_vm_uuid"]),
                str(item["destination_identity"]),
                str(item["protocol"]).casefold(),
                str(item["port_key"]),
                str(item["category"]),
            )
            saved_decision = decision_keys.get(key)
            decision = saved_decision.decision if saved_decision else "Not reviewed"
            readiness_status = dependency_readiness_status(
                None if decision == "Not reviewed" else decision,
                str(item["category"]),
            )
            review_counts[readiness_status] += 1
            review_items.append(
                {
                    "dependency": f"{item['source_vm']} → {item['destination']} ({item['destination_ip']}) {item['port_service']}",
                    "planner_decision": decision,
                    "readiness_status": readiness_status,
                    "planner_note": saved_decision.note if saved_decision else None,
                }
            )
        confidence_reasons = []
        if coverage < 0.8:
            confidence_reasons.append(f"Observation coverage is {coverage:.0%} of the selected window.")
        if inventory_age_days > 7:
            confidence_reasons.append(f"Inventory is {inventory_age_days:.0f} day(s) old.")
        if not collector_online:
            confidence_reasons.append("Collector is offline or has not recently checked in.")
        if not observations:
            confidence_reasons.append("No observations exist in the selected window.")
        confidence = "High" if not confidence_reasons else "Medium"
        if coverage < 0.4 or inventory_age_days > 30 or not observations:
            confidence = "Low"
        risk_reasons = []
        if review_counts["High impact"]:
            risk_reasons.append("One or more dependencies are high impact for cutover.")
        if review_counts["Action required"]:
            risk_reasons.append("One or more dependencies require cutover action.")
        if review_counts["Investigate"]:
            risk_reasons.append("One or more review dependencies require investigation.")
        risk = (
            "High"
            if review_counts["High impact"]
            else "Medium"
            if review_counts["Action required"] or review_counts["Investigate"]
            else "Low"
        )
        rows.append(
            {
                "wave": wave["wave"],
                "confidence": confidence,
                "confidence_reasons": confidence_reasons or ["Inventory and observation evidence meet current checks."],
                "risk": risk,
                "risk_reasons": risk_reasons or ["No unresolved review dependencies were found."],
                "review_dependency_count": len(wave_dependencies),
                "review_counts": review_counts,
                "review_items": review_items,
            }
        )
    return {"waves": rows}
