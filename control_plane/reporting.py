"""Collector-scoped connection reporting and migration-wave analysis."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from control_plane.models import InventoryVm, Observation

_BACKGROUND_UDP_SERVICES = {
    53: "DNS",
    67: "DHCP",
    68: "DHCP",
    123: "NTP",
    546: "DHCPv6",
    547: "DHCPv6",
}


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
) -> dict[str, object]:
    """Return one bounded page of evidence for the selected wave VMs.

    Filtering happens in the database before pagination.  This prevents a busy
    collector's newest global observations from hiding older evidence for a
    smaller selected wave.
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
    all_vms_by_uuid = {vm.vm_uuid: vm for vm in inventory}
    all_vms_by_ip = _vms_by_ip(inventory)
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


def migration_waves(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
) -> list[dict[str, object]]:
    """Build waves from connected active, powered-on VMs in one collector inventory."""

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
        }
        for index, component in enumerate(components, start=1)
    ]


def wave_summary(
    session: Session,
    collector_id: str,
    observed_after: datetime | None = None,
    observed_before: datetime | None = None,
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
    waves = migration_waves(session, collector_id, observed_after, observed_before)
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
