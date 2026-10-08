"""OCI-facing Streamlit dashboard for registered VMware collector agents."""

from __future__ import annotations

import html
from datetime import UTC, datetime, time, timedelta
from urllib.parse import urlencode

import streamlit as st

_GRAPH_EPHEMERAL_PORT_START = 32768
_GRAPH_EPHEMERAL_PORT_END = 65535
_NAVIGATION_PAGES = (
    "Agent Collector's Status",
    "Enroll Agent Collector",
    "VM/Server Inventory",
    "Migration Waves",
    "Dependency Review",
    "Migration Plan",
)
_DASHBOARD_NAVIGATION_PAGES = ("Current Environment", *_NAVIGATION_PAGES)
_PLANNER_DECISIONS_BY_CATEGORY = {
    "External dependency": (
        "Confirmed hard dependency",
        "Soft dependency",
        "External dependency accepted",
        "Network/firewall action required",
        "DNS/routing action required",
        "Owner validation required",
        "Not relevant / excluded",
    ),
    "Shared infrastructure": (
        "Confirmed shared service",
        "Confirmed hard dependency",
        "Soft dependency",
        "Target service setup required",
        "Owner validation required",
        "Not relevant / excluded",
    ),
    "Shared infrastructure candidate": (
        "Confirmed shared service",
        "Confirmed hard dependency",
        "Soft dependency",
        "Investigate before cutover",
        "Not relevant / excluded",
    ),
    "Unavailable internal VM": (
        "Confirmed hard dependency",
        "Soft dependency",
        "Requires hybrid plan",
        "Investigate before cutover",
        "Not relevant / excluded",
    ),
}

try:  # Supports `streamlit run control_plane/dashboard.py` from the project root.
    from control_plane.config import ControlPlaneSettings
    from control_plane.dashboard_client import ControlPlaneDashboardClient, DashboardClientError
    from control_plane.detailed_report import build_detailed_report, detailed_report_filename
    from control_plane.executive_report import build_executive_report, executive_report_filename
    from control_plane.plan_report import (
        build_migration_plan_excel,
        build_migration_plan_pdf,
        plan_report_filename,
    )
    from control_plane.reporting import dependency_readiness_status
except ModuleNotFoundError:  # pragma: no cover - Streamlit executes the file as a script.
    from config import ControlPlaneSettings
    from dashboard_client import ControlPlaneDashboardClient, DashboardClientError
    from detailed_report import build_detailed_report, detailed_report_filename
    from executive_report import build_executive_report, executive_report_filename
    from plan_report import (
        build_migration_plan_excel,
        build_migration_plan_pdf,
        plan_report_filename,
    )
    from reporting import dependency_readiness_status

st.set_page_config(page_title="Migration Discovery Dashboard", page_icon="🧭", layout="wide")
st.markdown(
    """
    <style>
    .summary-card { min-height: 122px; padding: 1rem; border: 1px solid rgba(255,255,255,.12);
      border-radius: 16px; background: linear-gradient(145deg, #263244, #141b27);
      box-shadow: 0 10px 24px rgba(0,0,0,.20), inset 0 1px 0 rgba(255,255,255,.08); }
    .summary-card__accent { width: 28px; height: 4px; margin-bottom: .8rem;
      border-radius: 9px; background: #5B9DFF; }
    .summary-card__label { color: #B7C5D6; font-size: .78rem; font-weight: 600; }
    .summary-card__value { margin-top: .35rem; color: #F7FAFC; font-size: 1.8rem;
      font-weight: 700; display: block; text-decoration: none; }
    .summary-card__value--critical, a.summary-card__value--critical:visited {
      color: #F87171 !important; }
    .summary-card__value--warning, a.summary-card__value--warning:visited {
      color: #FBBF24 !important; }
    .summary-card__value--success, a.summary-card__value--success:visited {
      color: #4ADE80 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


def card(
    label: str,
    value: int | str,
    href: str | None = None,
    critical: bool = False,
    warning: bool = False,
    success: bool = False,
) -> None:
    """Render a summary card while encoding text supplied by API responses."""

    display_value = f"{value:,}" if isinstance(value, int) else str(value)
    value_html = html.escape(display_value)
    value_class = "summary-card__value"
    if critical:
        value_class += " summary-card__value--critical"
    elif warning:
        value_class += " summary-card__value--warning"
    elif success:
        value_class += " summary-card__value--success"
    if href:
        value_html = (
            f'<a class="{value_class}" href="{html.escape(href, quote=True)}" '
            f'target="_self">{value_html}</a>'
        )
    else:
        value_html = f'<div class="{value_class}">{value_html}</div>'
    st.markdown(
        "<div class='summary-card'><div class='summary-card__accent'></div>"
        f"<div class='summary-card__label'>{html.escape(label)}</div>"
        f"{value_html}</div>",
        unsafe_allow_html=True,
    )


def _readiness_category(item: dict[str, object]) -> str:
    """Place every wave in one mutually exclusive readiness state."""

    risk = str(item.get("risk", ""))
    confidence = str(item.get("confidence", ""))
    if risk == "High":
        return "High risk"
    if risk == "Medium":
        return "Needs review"
    if risk == "Low" and confidence == "High":
        return "Ready for approval"
    return "Evidence needs refresh"


def _not_reviewed_dependency_count(items: list[dict[str, object]]) -> int:
    """Count review relationships without a saved planner decision."""

    return sum(not item.get("planner_decision") for item in items)


def _escape_dot(value: object) -> str:
    """Encode untrusted inventory values for a Graphviz quoted string."""

    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _compact_graph_label(value: object, limit: int = 18) -> str:
    """Keep the graph scannable while leaving full values in the details table."""

    label = str(value)
    return label if len(label) <= limit else f"{label[: limit - 3]}..."


def _compact_port_label(ports: set[str], limit: int = 2) -> str:
    """Bound one edge label while preserving all raw ports in the detail table."""

    shown = ", ".join(sorted(ports)[:limit])
    return shown if len(ports) <= limit else f"{shown} +{len(ports) - limit}"


def _planner_decisions(category: object) -> tuple[str, ...]:
    """Return only decisions meaningful for the selected review category."""

    return _PLANNER_DECISIONS_BY_CATEGORY.get(
        str(category),
        (
            "Confirmed hard dependency",
            "Soft dependency",
            "Investigate before cutover",
            "Not relevant / excluded",
        ),
    )


def _wave_graph_dimensions(
    vm_count: int, zoom_percent: int, connection_count: int = 0
) -> tuple[int, int]:
    """Give wave diagrams a readable canvas before applying the operator's zoom."""

    # A graph with only two wave VMs can still contain many external or inactive
    # dependency nodes.  Size for those relationships rather than just the wave
    # member count, while retaining an upper bound for large environments.
    if vm_count <= 2:
        base_width, base_height = 900, 460
    else:
        base_width, base_height = 1_100, 600
    visible_connections = min(max(connection_count, 0), 10)
    base_height = min(960, base_height + visible_connections * 35)
    return (
        int(base_width * zoom_percent / 100),
        int(base_height * zoom_percent / 100),
    )


def _remembered_selectbox(label: str, options: dict[str, str], state_key: str) -> str:
    """Keep a selection when this page is temporarily not rendered."""

    widget_key = f"_{state_key}"
    if state_key not in st.session_state or st.session_state[state_key] not in options:
        st.session_state[state_key] = next(iter(options))
    if widget_key not in st.session_state or st.session_state[widget_key] not in options:
        st.session_state[widget_key] = st.session_state[state_key]

    def save() -> None:
        st.session_state[state_key] = st.session_state[widget_key]

    return st.selectbox(label, options, key=widget_key, on_change=save)


def _plan_drift_card(drift: dict[str, object]) -> tuple[str, str] | None:
    """Summarize approved-plan drift without exposing VM detail on the waves page."""

    if drift.get("status") == "no_approved_plan":
        return None
    items = [item for item in drift.get("items", []) if isinstance(item, dict)]
    critical_count = sum(item.get("severity") == "Critical" for item in items)
    warning_count = sum(item.get("severity") == "Warning" for item in items)
    warning_label = "warning" if warning_count == 1 else "warnings"
    version = drift.get("plan_version")
    label = f"Approved plan drift (V{version})"
    if critical_count and warning_count:
        return label, f"{critical_count} Critical · {warning_count} {warning_label.title()}"
    if critical_count:
        return label, f"{critical_count} Critical"
    if warning_count:
        return label, f"{warning_count} {warning_label.title()}"
    return label, "Current"


def _plan_drift_href(collector_id: str, plan_version: int) -> str:
    """Create a relative, encoded dashboard URL for approved-plan drift detail."""

    return "?" + urlencode(
        {
            "page": "migration-plan",
            "collector_id": collector_id,
            "plan_version": plan_version,
        }
    )


def _remembered_slider(label: str, minimum: int, maximum: int, default: int, state_key: str) -> int:
    """Keep the graph zoom setting when the operator changes dashboard pages."""

    widget_key = f"_{state_key}"
    st.session_state.setdefault(state_key, default)
    if widget_key not in st.session_state:
        st.session_state[widget_key] = st.session_state[state_key]

    def save() -> None:
        st.session_state[state_key] = st.session_state[widget_key]

    return st.slider(
        label,
        min_value=minimum,
        max_value=maximum,
        step=10,
        format="%d%%",
        key=widget_key,
        on_change=save,
        help="Controls the graph canvas and the size of nodes, labels, and arrows.",
    )


def _observation_window(state_prefix: str = "wave") -> tuple[datetime, datetime, str]:
    """Return an inclusive UTC reporting window selected by the operator."""

    range_name = st.selectbox(
        "Observed connection time range",
        ("1 day", "5 days", "1 week", "1 month", "Custom time range"),
        key=f"{state_prefix}_time_range",
    )
    now = datetime.now(UTC)
    durations = {
        "1 day": timedelta(days=1),
        "5 days": timedelta(days=5),
        "1 week": timedelta(days=7),
        "1 month": timedelta(days=30),
    }
    if range_name in durations:
        return now - durations[range_name], now, range_name

    default_start = (now - timedelta(days=7)).date()
    start_date, end_date = st.date_input(
        "Custom observed dates",
        value=(default_start, now.date()),
        max_value=now.date(),
        key=f"{state_prefix}_custom_dates",
    )
    start = datetime.combine(start_date, time.min, tzinfo=UTC)
    end = datetime.combine(end_date, time.max, tzinfo=UTC)
    if end < start:
        st.error("The custom end date must be on or after the start date.")
        st.stop()
    return start, end, f"{start_date.isoformat()} to {end_date.isoformat()}"


def _wave_graph(
    wave: dict[str, object], connections: list[dict[str, object]], zoom_percent: int = 100
) -> str:
    """Create a bounded DOT graph for one wave without trusting inventory labels."""

    vm_uuids = list(wave["server_vm_uuids"])
    vm_names = list(wave["server_names"])
    names_by_uuid = dict(zip(vm_uuids, vm_names, strict=True))
    node_ids = {vm_uuid: f"vm_{index}" for index, vm_uuid in enumerate(vm_uuids, start=1)}
    inactive_dependencies = list(wave.get("inactive_internal_dependencies", []))
    inactive_by_uuid = {
        str(item["vm_uuid"]): item
        for item in inactive_dependencies
        if isinstance(item, dict) and isinstance(item.get("vm_uuid"), str)
    }
    inactive_node_ids = {
        vm_uuid: f"inactive_{index}" for index, vm_uuid in enumerate(inactive_by_uuid, start=1)
    }
    edge_ports: dict[tuple[str, str], set[str]] = {}
    edge_dynamic_protocols: dict[tuple[str, str], set[str]] = {}
    external_ids: dict[str, str] = {}

    for connection in connections:
        source_uuid = connection.get("source_vm_uuid")
        source_id = node_ids.get(source_uuid) or inactive_node_ids.get(source_uuid)
        if source_id is None:
            continue
        destination_uuid = connection.get("destination_vm_uuid")
        if destination_uuid in node_ids:
            destination_id = node_ids[destination_uuid]
        elif destination_uuid in inactive_node_ids:
            destination_id = inactive_node_ids[destination_uuid]
        else:
            destination_ip = str(connection.get("destination_ip", "unknown"))
            destination_id = external_ids.setdefault(
                destination_ip, f"external_{len(external_ids) + 1}"
            )
        edge = (source_id, destination_id)
        protocol = str(connection.get("protocol", "tcp"))
        port = connection.get("destination_port")
        if (
            isinstance(port, int)
            and _GRAPH_EPHEMERAL_PORT_START <= port <= _GRAPH_EPHEMERAL_PORT_END
        ):
            edge_dynamic_protocols.setdefault(edge, set()).add(protocol)
        else:
            edge_ports.setdefault(edge, set()).add(f"{protocol}/{port}")
        if len(set(edge_ports).union(edge_dynamic_protocols)) >= 100:
            break

    graph_scale = zoom_percent / 100
    graph_padding = 0.5 if len(vm_uuids) <= 2 else 0.25
    lines = [
        "digraph migration_wave {",
        "rankdir=LR;",
        f'graph [bgcolor="transparent", pad="{graph_padding * graph_scale:.2f}", '
        f'nodesep="{0.40 * graph_scale:.2f}", ranksep="{0.80 * graph_scale:.2f}", '
        'splines="polyline"];',
        (
            'node [shape=box, style="rounded,filled", fontname="Arial", '
            f'fontsize={11 * graph_scale:.1f}, margin="0.12,0.08", '
            f"width={2.05 * graph_scale:.2f}, height={0.55 * graph_scale:.2f}, fixedsize=true, "
            'color="#64748b", fillcolor="#dbeafe", fontcolor="#1f2937", penwidth=1.1];'
        ),
        (
            f'edge [fontname="Arial", fontsize={9 * graph_scale:.1f}, color="#64748b", '
            f'fontcolor="#64748b", arrowsize={0.75 * graph_scale:.2f}, penwidth=1.0];'
        ),
    ]
    for vm_uuid, node_id in node_ids.items():
        lines.append(
            f'{node_id} [label="{_escape_dot(_compact_graph_label(names_by_uuid[vm_uuid]))}"];'
        )
    for vm_uuid, node_id in inactive_node_ids.items():
        dependency = inactive_by_uuid[vm_uuid]
        name = _escape_dot(_compact_graph_label(dependency.get("name", vm_uuid)))
        lines.append(
            f'{node_id} [label="Unavailable\\n{name}", style="rounded,dashed,filled", '
            'color="#a47527", fillcolor="#fef3c7", fontcolor="#3b2f10"];'
        )
    for destination_ip, node_id in external_ids.items():
        lines.append(
            f'{node_id} [label="External\\n{_escape_dot(_compact_graph_label(destination_ip))}", '
            'color="#a47527", fillcolor="#fef3c7", fontcolor="#3b2f10"];'
        )
    for edge in set(edge_ports).union(edge_dynamic_protocols):
        source_id, destination_id = edge
        ports = edge_ports.get(edge, set()).copy()
        for protocol in edge_dynamic_protocols.get(edge, set()):
            if not any(label.startswith(f"{protocol}/") for label in ports):
                ports.add(protocol)
        label = _compact_port_label(ports)
        lines.append(f'{source_id} -> {destination_id} [label="{_escape_dot(label)}"];')
    lines.append("}")
    return "\n".join(lines)


def _connection_vm_uuids(connection: dict[str, object]) -> set[str]:
    """Return current-inventory VM identifiers referenced by one observation."""

    return {
        str(vm_uuid)
        for vm_uuid in (connection.get("source_vm_uuid"), connection.get("destination_vm_uuid"))
        if isinstance(vm_uuid, str) and vm_uuid
    }


def _dependency_map_graph(
    names_by_uuid: dict[str, str],
    connections: list[dict[str, object]],
    displayed_vm_uuids: set[str],
    focus_vm_uuid: str | None = None,
    zoom_percent: int = 100,
) -> str:
    """Build a compact discovery map with external endpoints collapsed to one node."""

    node_ids = {
        vm_uuid: f"vm_{index}"
        for index, vm_uuid in enumerate(sorted(displayed_vm_uuids), start=1)
    }
    edge_ports: dict[tuple[str, str], set[str]] = {}
    external_destinations: set[str] = set()
    for connection in connections:
        source_vm_uuid = connection.get("source_vm_uuid")
        destination_vm_uuid = connection.get("destination_vm_uuid")
        if not isinstance(source_vm_uuid, str) or source_vm_uuid not in node_ids:
            continue
        if isinstance(destination_vm_uuid, str) and destination_vm_uuid in node_ids:
            destination_id = node_ids[destination_vm_uuid]
        elif destination_vm_uuid:
            continue
        else:
            destination_ip = str(connection.get("destination_ip", "Unknown"))
            external_destinations.add(destination_ip)
            destination_id = "external_endpoints"
        edge = (node_ids[source_vm_uuid], destination_id)
        protocol = str(connection.get("protocol", "tcp"))
        port = connection.get("destination_port")
        if (
            isinstance(port, int)
            and _GRAPH_EPHEMERAL_PORT_START <= port <= _GRAPH_EPHEMERAL_PORT_END
        ):
            edge_ports.setdefault(edge, set()).add(protocol)
        else:
            edge_ports.setdefault(edge, set()).add(f"{protocol}/{port}")
        if len(edge_ports) >= 100:
            break

    graph_scale = zoom_percent / 100
    lines = [
        "digraph dependency_map {",
        "rankdir=LR;",
        'graph [bgcolor="transparent", pad="0.18", nodesep="0.42", ranksep="0.85", '
        'splines="polyline"];',
        (
            'node [shape=box, style="rounded,filled", fontname="Arial", '
            f'fontsize={9 * graph_scale:.1f}, margin="0.06,0.04", '
            f'width={1.65 * graph_scale:.2f}, height={0.45 * graph_scale:.2f}, '
            'fixedsize=true, color="#64748b", fillcolor="#e2e8f0", '
            'fontcolor="#0f172a", penwidth=1.1];'
        ),
        (
            f'edge [fontname="Arial", fontsize={8 * graph_scale:.1f}, color="#64748b", '
            f'fontcolor="#94a3b8", arrowsize={0.65 * graph_scale:.2f}, penwidth=1.0];'
        ),
    ]
    for vm_uuid, node_id in node_ids.items():
        label = _escape_dot(_compact_graph_label(names_by_uuid.get(vm_uuid, vm_uuid), 16))
        if vm_uuid == focus_vm_uuid:
            lines.append(
                f'{node_id} [label="{label}", color="#2563eb", fillcolor="#dbeafe", '
                'penwidth=2.0];'
            )
        else:
            lines.append(f'{node_id} [label="{label}"];')
    if external_destinations:
        lines.append(
            'external_endpoints [label="External endpoints\\n'
            f'{len(external_destinations)} destination(s)", color="#a47527", '
            'fillcolor="#fef3c7", fontcolor="#3b2f10"];'
        )
    for edge, ports in edge_ports.items():
        source_id, destination_id = edge
        lines.append(
            f'{source_id} -> {destination_id} '
            f'[label="{_escape_dot(_compact_port_label(ports))}"];'
        )
    lines.append("}")
    return "\n".join(lines)


def _connection_display_name(connection: dict[str, object], side: str) -> str:
    """Prefer human-readable connection names while retaining a safe fallback."""

    return str(
        connection.get(f"{side}_vm_name")
        or connection.get(f"{side}_name")
        or connection.get(f"{side}_vm_uuid")
        or connection.get("destination_ip", "Unknown")
    )


def render_current_environment(collectors: list[dict[str, object]]) -> None:
    """Render a read-only view of discovered inventory and observed traffic."""

    st.subheader("Current Environment")
    st.write(
        "See the current discovered VM estate and observed network traffic. "
        "Recommended waves below are automatic discovery output, not a migration plan."
    )
    if not collectors:
        st.info("No registered collectors are available yet.")
        return

    options = {
        f"{item['display_name']} - {item['tenant_id']} - {item['collector_id']}": item[
            "collector_id"
        ]
        for item in collectors
    }
    selected_label = _remembered_selectbox(
        "Collector", options, "current_environment_collector"
    )
    observed_after, observed_before, range_label = _observation_window("current_environment")
    collector_id = options[selected_label]
    try:
        inventory = client.inventory(collector_id)
        connections = client.connections(
            collector_id, observed_after.isoformat(), observed_before.isoformat()
        )
        waves = client.waves(
            collector_id,
            observed_after.isoformat(),
            observed_before.isoformat(),
            use_approved_plan=False,
        )
        summary = client.wave_summary(
            collector_id,
            observed_after.isoformat(),
            observed_before.isoformat(),
            use_approved_plan=False,
        )
    except DashboardClientError as error:
        st.error(str(error))
        return

    st.caption(
        f"Observed traffic window: {range_label}. Recommended waves use the current "
        "automatic recommendation. Graphs include at most 100 unique edges."
    )

    inventory_vm_uuids = {
        str(item["vm_uuid"]) for item in inventory if isinstance(item.get("vm_uuid"), str)
    }
    inventory_names_by_uuid = {
        str(item["vm_uuid"]): str(item.get("vm_name") or item["vm_uuid"])
        for item in inventory
        if isinstance(item.get("vm_uuid"), str)
    }
    observed_vm_uuids = set().union(
        *(_connection_vm_uuids(item) for item in connections),
    ) if connections else set()
    observed_inventory_vm_uuids = inventory_vm_uuids.intersection(observed_vm_uuids)
    st.markdown("#### VM estate")
    estate_cards = st.columns(5)
    estate_values = (
        ("Active inventory VMs", len(inventory)),
        ("Powered-on VMs", sum(item.get("power_state") == "poweredOn" for item in inventory)),
        ("Powered-off VMs", sum(item.get("power_state") == "poweredOff" for item in inventory)),
        ("VMs with observed traffic", len(observed_inventory_vm_uuids)),
        ("VMs without observed traffic", len(inventory_vm_uuids - observed_inventory_vm_uuids)),
    )
    for column, (label, value) in zip(estate_cards, estate_values, strict=True):
        with column:
            card(label, value)

    st.markdown("#### Recommended initial migration waves")
    st.caption(
        "These groups are generated from the selected inventory and observed traffic window. "
        "They are not an approved migration plan."
    )
    wave_cards = st.columns(3)
    wave_values = (
        ("Recommended waves", int(summary.get("wave_count", len(waves)))),
        (
            "Waves with VM-to-VM connections",
            int(summary.get("waves_with_observed_connections", 0)),
        ),
        (
            "Waves without VM-to-VM connections",
            int(summary.get("waves_without_observed_connections", 0)),
        ),
    )
    for column, (label, value) in zip(wave_cards, wave_values, strict=True):
        with column:
            card(label, value)

    map_view = st.radio(
        "Map view",
        ("Recommended wave", "Focused VM"),
        horizontal=True,
        key="current_environment_map_view",
        help="Recommended wave shows one automatic group. Focused VM shows one VM and its "
        "directly observed connections.",
    )
    zoom_percent = _remembered_slider(
        "Graph zoom", 70, 200, 100, "current_environment_graph_zoom"
    )
    connection_scope_vm_uuids: list[str] = []
    connection_scope_label = ""
    if map_view == "Recommended wave":
        if not waves:
            st.info("No automatic migration-wave recommendation is available for this inventory.")
        else:
            wave_options = {
                f"Wave {wave['wave']} - {len(wave['server_names'])} VM(s)": index
                for index, wave in enumerate(waves)
            }
            selected_wave_label = _remembered_selectbox(
                "Recommended wave", wave_options, "current_environment_wave"
            )
            selected_wave = waves[wave_options[selected_wave_label]]
            selected_vm_uuids = {
                str(vm_uuid) for vm_uuid in selected_wave["server_vm_uuids"]
            }
            connection_scope_vm_uuids = list(selected_vm_uuids)
            connection_scope_label = selected_wave_label
            st.caption(str(selected_wave.get("reason", "Automatic recommendation.")))
            st.caption(
                "Blue/grey nodes are discovered VMs. Gold is a collapsed group of external "
                "destinations; use the observed-connections table for individual IP details."
            )
            st.graphviz_chart(
                _dependency_map_graph(
                    inventory_names_by_uuid,
                    connections,
                    selected_vm_uuids,
                    zoom_percent=zoom_percent,
                ),
                width="content",
            )
    elif inventory_names_by_uuid:
        focus_options = {
            name: vm_uuid for vm_uuid, name in sorted(inventory_names_by_uuid.items(), key=lambda x: x[1])
        }
        focus_label = _remembered_selectbox(
            "Focus VM", focus_options, "current_environment_focus_vm"
        )
        focus_vm_uuid = focus_options[focus_label]
        connection_scope_vm_uuids = [focus_vm_uuid]
        connection_scope_label = focus_label
        focused_connections = [
            item
            for item in connections
            if focus_vm_uuid in _connection_vm_uuids(item)
        ]
        displayed_vm_uuids = {focus_vm_uuid}
        for connection in focused_connections:
            displayed_vm_uuids.update(
                _connection_vm_uuids(connection).intersection(inventory_vm_uuids)
            )
        st.caption(
            "The selected VM is blue. Related discovered VMs are grey; external destinations "
            "are collapsed into one gold node."
        )
        st.graphviz_chart(
            _dependency_map_graph(
                inventory_names_by_uuid,
                focused_connections,
                displayed_vm_uuids,
                focus_vm_uuid=focus_vm_uuid,
                zoom_percent=zoom_percent,
            ),
            width="content",
        )
    else:
        st.info("No current inventory VMs are available to show in the focused map.")

    st.markdown(
        f"#### Current inventory — {connection_scope_label}"
        if connection_scope_label
        else "#### Current inventory"
    )
    inventory_search = st.text_input(
        "Filter inventory by VM name, IP, cluster, folder, or operating system",
        key="current_environment_inventory_search",
    ).casefold().strip()
    scoped_inventory = [
        item
        for item in inventory
        if str(item.get("vm_uuid", "")) in connection_scope_vm_uuids
    ]
    inventory_rows = [
        {
            "VM": item.get("vm_name", ""),
            "Hostname": item.get("hostname", ""),
            "IP addresses": item.get("ips", ""),
            "Power state": item.get("power_state", ""),
            "Cluster": item.get("cluster", ""),
            "Folder": item.get("folder", ""),
            "Operating system": item.get("os_name", ""),
            "Traffic observed": (
                "Yes" if str(item.get("vm_uuid", "")) in observed_inventory_vm_uuids else "No"
            ),
        }
        for item in scoped_inventory
    ]
    if inventory_search:
        inventory_rows = [
            row
            for row in inventory_rows
            if inventory_search in " ".join(str(value) for value in row.values()).casefold()
        ]
    if inventory_rows:
        st.dataframe(inventory_rows, width="stretch", hide_index=True)
    else:
        st.info("No inventory VMs match the selected wave or focused VM.")

    st.markdown("#### Current observed connections")
    if not connection_scope_vm_uuids:
        st.info("Select a recommended wave or focused VM to view its observed connections.")
        return

    page_state_key = (
        f"current_environment_connection_page_{collector_id}_{map_view}_{connection_scope_label}"
    )
    st.session_state.setdefault(page_state_key, 1)
    try:
        scoped_connection_page = client.wave_connections(
            collector_id,
            connection_scope_vm_uuids,
            observed_after.isoformat(),
            observed_before.isoformat(),
            st.session_state[page_state_key],
            deduplicate=True,
            exclude_dynamic_private_ports=True,
        )
    except DashboardClientError as error:
        st.error(str(error))
        return

    scoped_connections = scoped_connection_page["items"]
    total_connections = scoped_connection_page["total"]
    current_page = scoped_connection_page["page"]
    page_size = scoped_connection_page["page_size"]
    st.caption(f"Showing {total_connections} unique connection(s) for {connection_scope_label}.")
    connection_search = st.text_input(
        "Filter connections by source, destination, IP, protocol, or port",
        key="current_environment_connection_search",
    ).casefold().strip()
    connection_rows = [
        {
            "Source VM": item.get("source_vm_name", ""),
            "Destination": (
                item.get("destination_hostname")
                or item.get("destination_vm_name")
                or item.get("destination_ip", "")
            ),
            "Destination IP": item.get("destination_ip", ""),
            "Connection type": "Internal" if item.get("destination_vm_uuid") else "External",
            "Protocol": str(item.get("protocol", "")).upper(),
            "Port": item.get("destination_port", ""),
            "Traffic classification": item.get("traffic_class", ""),
            "Evidence": item.get("evidence", ""),
            "Observations": item.get("observation_count", ""),
        }
        for item in scoped_connections
    ]
    if connection_search:
        connection_rows = [
            row
            for row in connection_rows
            if connection_search in " ".join(str(value) for value in row.values()).casefold()
    ]
    st.dataframe(connection_rows, width="stretch", hide_index=True)
    total_pages = max(1, (total_connections + page_size - 1) // page_size)
    if total_pages > 1:
        previous, page_label, next_page = st.columns((1, 3, 1))
        with previous:
            if st.button("Previous", key=f"current_environment_previous_{page_state_key}"):
                st.session_state[page_state_key] = max(1, current_page - 1)
                st.rerun()
        with page_label:
            st.caption(
                f"Page {current_page} of {total_pages}; showing up to {page_size} rows per page."
            )
        with next_page:
            if st.button("Next", key=f"current_environment_next_{page_state_key}"):
                st.session_state[page_state_key] = min(total_pages, current_page + 1)
                st.rerun()


def render_migration_waves(collectors: list[dict[str, object]]) -> None:
    """Render one collector's reporting window without resetting dashboard choices."""

    st.subheader("Migration waves")
    st.write(
        "Review TCP connections uploaded by one collector. Only currently powered-on VMs "
        "form move groups; unavailable internal VMs remain visible as review dependencies."
    )
    if not collectors:
        st.info("No registered collectors are available yet.")
        return
    action_controls = st.empty()
    options = {
        f"{item['display_name']} - {item['tenant_id']} - {item['collector_id']}": item[
            "collector_id"
        ]
        for item in collectors
    }
    selected_label = _remembered_selectbox("Collector", options, "wave_collector")
    observed_after, observed_before, range_label = _observation_window()
    selected_collector_id = options[selected_label]
    try:
        plans = client.migration_plans(selected_collector_id)
    except DashboardClientError as error:
        st.error(str(error))
        return
    approved_plan = next((plan for plan in plans if plan["status"] == "approved"), None)
    display_options = {"Automatic recommendation": "recommended"}
    if approved_plan is not None:
        display_options = {
            f"Latest approved plan (V{approved_plan['version']})": "approved",
            **display_options,
        }
    selected_display = _remembered_selectbox(
        "Display", display_options, "wave_display"
    )
    use_approved_plan = display_options[selected_display] == "approved"
    try:
        summary = client.wave_summary(
            selected_collector_id,
            observed_after.isoformat(),
            observed_before.isoformat(),
            use_approved_plan=use_approved_plan,
        )
        waves = client.waves(
            selected_collector_id,
            observed_after.isoformat(),
            observed_before.isoformat(),
            use_approved_plan=use_approved_plan,
        )
        readiness = client.wave_readiness(
            selected_collector_id,
            observed_after.isoformat(),
            observed_before.isoformat(),
            use_approved_plan=use_approved_plan,
        )
        plan_drift = client.plan_drift(selected_collector_id)
        connections = client.connections(
            selected_collector_id, observed_after.isoformat(), observed_before.isoformat()
        )
    except DashboardClientError as error:
        st.error(str(error))
        return

    st.caption(
        f"Reporting window: {range_label}. Display: {selected_display}. Graphs include at most "
        "100 unique edges. "
        "| Potential ephemeral ports (32768-65535) are hidden from graph labels; "
        "full port values remain in the observed-connections table."
    )
    selected_collector = next(
        item for item in collectors if item["collector_id"] == selected_collector_id
    )
    try:
        report_created_at = datetime.now(UTC)
        executive_report = build_executive_report(
            selected_collector, summary, waves, range_label, report_created_at
        )
        detailed_connections = client.detailed_connections(
            selected_collector_id, observed_after.isoformat(), observed_before.isoformat()
        )
        detailed_report = build_detailed_report(
            selected_collector, waves, detailed_connections, range_label
        )
        with action_controls.container():
            guidance_column, action_group = st.columns((5, 3), gap="small")
            with guidance_column:
                st.caption("Use Refresh now to load the latest inventory and connection data.")
            with action_group:
                with st.container(
                    horizontal=True, horizontal_alignment="right", gap="small"
                ):
                    st.button(
                        "Refresh now",
                        key="refresh_migration_waves",
                        type="primary",
                        icon="🔄",
                        width="content",
                        help="Reload the latest inventory and connection data.",
                    )
                    st.download_button(
                        "Executive report (PDF)",
                        data=executive_report,
                        file_name=executive_report_filename(
                            selected_collector["display_name"], report_created_at
                        ),
                        mime="application/pdf",
                        key=f"executive_report_{selected_collector_id}_{range_label}",
                        type="primary",
                        icon="⬇️",
                        width="content",
                        help="Download the executive migration-wave summary as a PDF.",
                    )
                    st.download_button(
                        "Detailed report (Excel)",
                        data=detailed_report,
                        file_name=detailed_report_filename(
                            selected_collector["display_name"], report_created_at
                        ),
                        mime=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                        key=f"detailed_report_{selected_collector_id}_{range_label}",
                        type="primary",
                        icon="⬇️",
                        width="content",
                        help=(
                            "Download deduplicated TCP/UDP connection evidence as an Excel "
                            "workbook."
                        ),
                    )
    except (DashboardClientError, KeyError, TypeError, ValueError):
        with action_controls.container():
            guidance_column, refresh_column = st.columns((7, 1), gap="small")
            with guidance_column:
                st.caption("Use Refresh now to load the latest inventory and connection data.")
            with refresh_column:
                st.button(
                    "Refresh now",
                    key="refresh_migration_waves",
                    type="primary",
                    icon="🔄",
                    width="content",
                    help="Reload the latest inventory and connection data.",
                )
        st.warning("The selected reports could not be generated for the reporting window.")
    summary_cards: list[tuple[str, int | str]] = [
        ("Migration waves", summary["wave_count"]),
        ("Active inventory VMs", summary["active_inventory_vm_count"]),
        ("Eligible powered-on VMs", summary["eligible_vm_count"]),
        ("Waves with VM-to-VM connections", summary["waves_with_observed_connections"]),
        (
            "Waves without VM-to-VM connections",
            summary["waves_without_observed_connections"],
        ),
    ]
    cards = st.columns(len(summary_cards))
    for column, (label, value) in zip(cards, summary_cards, strict=True):
        with column:
            card(label, value)
    readiness_rows = list(readiness["waves"])
    st.markdown("#### Wave readiness")
    readiness_filter_key = f"wave_readiness_filter_{selected_collector_id}"
    st.session_state.setdefault(readiness_filter_key, "All waves")
    drift_card = _plan_drift_card(plan_drift)
    readiness_categories = (
        "Ready for approval",
        "Evidence needs refresh",
        "Needs review",
        "High risk",
    )
    readiness_filters = (("All waves", len(readiness_rows)),) + tuple(
        (
            label,
            sum(1 for item in readiness_rows if _readiness_category(item) == label),
        )
        for label in readiness_categories
    )
    readiness_cards = st.columns(len(readiness_filters) + (1 if drift_card else 0))
    for column, (label, value) in zip(
        readiness_cards[: len(readiness_filters)], readiness_filters, strict=True
    ):
        with column:
            card(
                label,
                value,
                critical=label == "High risk" and value > 0,
                warning=label in {"Evidence needs refresh", "Needs review"} and value > 0,
                success=label == "Ready for approval" and value > 0,
            )
    if drift_card is not None:
        plan_version = plan_drift.get("plan_version")
        drift_href = (
            _plan_drift_href(selected_collector_id, plan_version)
            if isinstance(plan_version, int)
            else None
        )
        with readiness_cards[-1]:
            card(
                drift_card[0],
                drift_card[1],
                href=drift_href,
                critical="Critical" in drift_card[1],
            )
    previous_readiness_filter = st.session_state[readiness_filter_key]
    st.radio(
        "Filter readiness table",
        tuple(label for label, _ in readiness_filters),
        horizontal=True,
        key=readiness_filter_key,
        label_visibility="collapsed",
    )
    if st.session_state[readiness_filter_key] != previous_readiness_filter:
        st.session_state[f"wave_readiness_page_{selected_collector_id}"] = 1
    readiness_by_wave = {item["wave"]: item for item in readiness_rows}
    readiness_table_rows = [
        {
            "Wave": f"Wave {wave['wave']} - {len(wave['server_names'])} VM(s)",
            "Readiness": _readiness_category(readiness_by_wave[wave["wave"]]),
            "Confidence": readiness_by_wave[wave["wave"]]["confidence"],
            "Risk": readiness_by_wave[wave["wave"]]["risk"],
            "Review dependencies": readiness_by_wave[wave["wave"]]["review_dependency_count"],
            "Action required": readiness_by_wave[wave["wave"]]["review_counts"]["Action required"],
            "Investigate": readiness_by_wave[wave["wave"]]["review_counts"]["Investigate"],
            "High impact": readiness_by_wave[wave["wave"]]["review_counts"]["High impact"],
            "Main reason": readiness_by_wave[wave["wave"]]["risk_reasons"][0],
        }
        for wave in waves
    ]
    selected_readiness_filter = st.session_state[readiness_filter_key]
    if selected_readiness_filter != "All waves":
        readiness_table_rows = [
            row for row in readiness_table_rows if row["Readiness"] == selected_readiness_filter
        ]
    st.caption(f"Showing: {selected_readiness_filter}")
    readiness_page_size = 20
    readiness_page_key = f"wave_readiness_page_{selected_collector_id}"
    st.session_state.setdefault(readiness_page_key, 1)
    readiness_page_count = max(
        1, (len(readiness_table_rows) + readiness_page_size - 1) // readiness_page_size
    )
    readiness_page = min(st.session_state[readiness_page_key], readiness_page_count)
    st.session_state[readiness_page_key] = readiness_page
    start = (readiness_page - 1) * readiness_page_size
    st.dataframe(
        readiness_table_rows[start : start + readiness_page_size],
        width="stretch",
        hide_index=True,
    )
    if readiness_page_count > 1:
        previous, page_label, next_page = st.columns((1, 3, 1))
        with previous:
            if st.button("Previous", key="readiness_previous"):
                st.session_state[readiness_page_key] = max(1, readiness_page - 1)
                st.rerun()
        with page_label:
            st.caption(
                f"Page {readiness_page} of {readiness_page_count}; "
                f"showing {min(readiness_page_size, len(readiness_table_rows) - start)} of "
                f"{len(readiness_table_rows)} waves."
            )
        with next_page:
            if st.button("Next", key="readiness_next"):
                st.session_state[readiness_page_key] = min(readiness_page_count, readiness_page + 1)
                st.rerun()
    if not waves:
        st.info("No eligible powered-on VMs are available in this collector inventory.")
        return
    wave_options = {
        (
            f"Wave {wave['wave']} - {len(wave['server_names'])} VM(s) "
            f"[{readiness_by_wave[wave['wave']]['risk']} risk]"
        ): index
        for index, wave in enumerate(waves)
    }
    selected_wave_label = _remembered_selectbox("Migration wave", wave_options, "selected_wave")
    selected_wave = waves[wave_options[selected_wave_label]]
    selected_readiness = readiness_by_wave[selected_wave["wave"]]
    st.divider()
    st.markdown(f"#### {html.escape(selected_wave_label)}")
    st.caption(str(selected_wave["reason"]))
    confidence_column, risk_column = st.columns(2)
    with confidence_column:
        st.info(
            "Confidence: "
            f"{selected_readiness['confidence']} — "
            + " ".join(selected_readiness["confidence_reasons"])
        )
    with risk_column:
        risk_method = (
            st.error
            if selected_readiness["risk"] == "High"
            else st.warning
            if selected_readiness["risk"] == "Medium"
            else st.success
        )
        risk_method(
            f"Risk: {selected_readiness['risk']} — " + " ".join(selected_readiness["risk_reasons"])
        )
    review_counts = selected_readiness["review_counts"]
    st.caption(
        "Dependency review status — "
        f"Resolved: {review_counts['Resolved']} | "
        f"Advisory: {review_counts['Advisory']} | "
        f"Action required: {review_counts['Action required']} | "
        f"Investigate: {review_counts['Investigate']} | "
        f"High impact: {review_counts['High impact']}"
    )
    if selected_readiness["review_items"]:
        with st.expander("Dependency decisions affecting wave readiness"):
            st.dataframe(
                [
                    {
                        "Dependency": item["dependency"],
                        "Planner decision": item["planner_decision"],
                        "Readiness status": item["readiness_status"],
                        "Planner note": item["planner_note"] or "",
                    }
                    for item in selected_readiness["review_items"]
                ],
                width="stretch",
                hide_index=True,
            )
    zoom_controls, _ = st.columns((2, 5))
    with zoom_controls:
        diagram_zoom = _remembered_slider("Graph zoom", 80, 200, 100, "wave_graph_zoom")
    diagram_width, diagram_height = _wave_graph_dimensions(
        len(selected_wave["server_vm_uuids"]), diagram_zoom, len(connections)
    )
    st.graphviz_chart(
        _wave_graph(selected_wave, connections, diagram_zoom),
        width=diagram_width,
        height=diagram_height,
    )

    page_state_key = f"wave_connection_page_{selected_collector_id}_{selected_wave['wave']}"
    st.session_state.setdefault(page_state_key, 1)
    try:
        wave_connection_page = client.wave_connections(
            selected_collector_id,
            list(selected_wave["server_vm_uuids"]),
            observed_after.isoformat(),
            observed_before.isoformat(),
            st.session_state[page_state_key],
        )
    except DashboardClientError as error:
        st.error(str(error))
        return
    wave_connections = wave_connection_page["items"]
    total_connections = wave_connection_page["total"]
    current_page = wave_connection_page["page"]
    page_size = wave_connection_page["page_size"]
    with st.expander(f"Observed connections for {selected_wave_label} ({total_connections})"):
        if wave_connections:
            display_connections = [
                {
                    "Source VM": connection.get("source_vm_name"),
                    "Destination": (
                        connection.get("destination_hostname")
                        or connection.get("destination_vm_name")
                        or connection.get("destination_ip")
                    ),
                    "Destination IP": connection.get("destination_ip"),
                    "Port": connection.get("destination_port"),
                    "Protocol": connection.get("protocol"),
                    "Traffic classification": connection.get("traffic_class"),
                    "Observed at": connection.get("observed_at"),
                }
                for connection in wave_connections
            ]
            st.dataframe(display_connections, width="stretch", hide_index=True)
            total_pages = max(1, (total_connections + page_size - 1) // page_size)
            if total_pages > 1:
                previous, page_label, next_page = st.columns((1, 3, 1))
                with previous:
                    if st.button("Previous", key=f"wave_previous_{page_state_key}"):
                        st.session_state[page_state_key] = max(1, current_page - 1)
                        st.rerun()
                with page_label:
                    st.caption(
                        f"Page {current_page} of {total_pages}; "
                        f"showing up to {page_size} observations per page."
                    )
                with next_page:
                    if st.button("Next", key=f"wave_next_{page_state_key}"):
                        st.session_state[page_state_key] = min(total_pages, current_page + 1)
                        st.rerun()
        else:
            st.info("No observations were recorded for this wave in the selected time range.")


def render_migration_plan(collectors: list[dict[str, object]]) -> None:
    """Create, revise, and approve a versioned planner-owned migration plan."""

    st.subheader("Migration Plan")
    st.write(
        "The automatic TCP grouping remains the recommendation. A draft plan is a planner-owned "
        "snapshot that can be changed before approval; approved versions are read-only."
    )
    if not collectors:
        st.info("No registered collectors are available yet.")
        return
    options = {
        f"{item['display_name']} - {item['tenant_id']} - {item['collector_id']}": item[
            "collector_id"
        ]
        for item in collectors
    }
    requested_collector_id = str(st.query_params.get("collector_id", ""))
    requested_plan_version = st.query_params.get("plan_version")
    requested_collector_label = next(
        (
            label
            for label, collector_id in options.items()
            if collector_id == requested_collector_id
        ),
        None,
    )
    if requested_collector_label is not None:
        st.session_state["plan_collector"] = requested_collector_label
        st.session_state["_plan_collector"] = requested_collector_label
    selected_label = _remembered_selectbox("Collector", options, "plan_collector")
    collector_id = options[selected_label]
    observed_after, observed_before, range_label = _observation_window()
    try:
        plans = client.migration_plans(collector_id)
    except DashboardClientError as error:
        st.error(str(error))
        return

    draft_exists = any(plan["status"] == "draft" for plan in plans)
    create_col, clone_col = st.columns(2)
    with create_col:
        with st.expander("Create draft from current recommendation", expanded=not plans):
            if draft_exists:
                st.info("A draft already exists. Edit or approve it before creating another draft.")
            else:
                with st.form("create_migration_plan"):
                    planner_name = st.text_input("Planner name", key="create_plan_planner")
                    plan_note = st.text_area("Plan note (optional)", key="create_plan_note")
                    create = st.form_submit_button("Create draft plan", type="primary")
                if create:
                    try:
                        client.create_migration_plan(
                            collector_id,
                            {"planner_name": planner_name, "note": plan_note},
                            observed_after.isoformat(),
                            observed_before.isoformat(),
                        )
                        st.success("Draft plan created from the current automatic recommendation.")
                        st.rerun()
                    except DashboardClientError as error:
                        st.error(str(error))
    with clone_col:
        with st.expander("Create a new version from an approved plan"):
            approved_plans = [plan for plan in plans if plan["status"] == "approved"]
            if draft_exists or not approved_plans:
                st.info("Approve a plan first, and ensure no draft plan is open.")
            else:
                approved_options = {
                    f"Version {plan['version']} - approved": plan["plan_id"]
                    for plan in approved_plans
                }
                with st.form("clone_migration_plan"):
                    source_label = st.selectbox("Approved plan", approved_options)
                    planner_name = st.text_input("Planner name", key="clone_plan_planner")
                    plan_note = st.text_area("Version note (optional)", key="clone_plan_note")
                    clone = st.form_submit_button("Create draft version", type="primary")
                if clone:
                    try:
                        client.clone_migration_plan(
                            collector_id,
                            approved_options[source_label],
                            {"planner_name": planner_name, "note": plan_note},
                        )
                        st.success("New draft version created from the approved plan.")
                        st.rerun()
                    except DashboardClientError as error:
                        st.error(str(error))

    if not plans:
        st.caption(f"Recommendation window for a new plan: {range_label}.")
        return
    plan_options = {
        f"Version {plan['version']} - {plan['status'].title()}": plan["plan_id"] for plan in plans
    }
    requested_plan_label = f"Version {requested_plan_version} - Approved"
    if requested_collector_label is not None and requested_plan_label in plan_options:
        st.session_state["selected_plan"] = requested_plan_label
        st.session_state["_selected_plan"] = requested_plan_label
        st.query_params.clear()
    selected_plan_label = _remembered_selectbox("Plan version", plan_options, "selected_plan")
    try:
        plan = client.migration_plan(collector_id, plan_options[selected_plan_label])
        drift = client.plan_drift(collector_id) if plan["status"] == "approved" else None
    except DashboardClientError as error:
        st.error(str(error))
        return
    assignments = list(plan["assignments"])
    included = [item for item in assignments if item["disposition"] == "included"]
    excluded = [item for item in assignments if item["disposition"] == "excluded"]
    waves = {item["wave_number"] for item in included if item["wave_number"] is not None}
    cards = st.columns(4)
    for column, (label, value) in zip(
        cards,
        (
            ("Plan version", plan["version"]),
            ("Included VMs", len(included)),
            ("Planned waves", len(waves)),
            ("Excluded VMs", len(excluded)),
        ),
        strict=True,
    ):
        with column:
            card(label, int(value))
    st.caption(
        f"Status: {plan['status'].title()}. Created by {plan['planner_name']}. "
        f"Approved plans become the effective waves used by readiness and exports."
    )
    if drift is not None:
        if drift["status"] == "current":
            st.success("Plan drift: no inventory membership drift was detected.")
        else:
            st.warning(f"Plan drift requires review: {len(drift['items'])} item(s) found.")
            st.dataframe(drift["items"], width="stretch", hide_index=True)

    st.markdown("#### Export selected plan version")
    st.caption(
        "These exports record the selected plan version. Draft and superseded versions are "
        "clearly marked and are not approved for execution. If drift exists, the PDF includes "
        "a dedicated Plan drift detail page."
    )
    selected_collector = next(item for item in collectors if item["collector_id"] == collector_id)
    included_vm_uuids = {str(item["vm_uuid"]) for item in included}
    plan_dependencies: list[dict[str, object]] = []
    plan_readiness: list[dict[str, object]] = []
    try:
        dependency_items = client.dependencies(
            collector_id, observed_after.isoformat(), observed_before.isoformat()
        )["items"]
        plan_dependencies = [
            item
            for item in dependency_items
            if str(item["source_vm_uuid"]) in included_vm_uuids
            or str(item.get("destination_vm_uuid") or "") in included_vm_uuids
        ]
        if plan["status"] == "approved":
            plan_waves = {
                int(item["wave_number"]) for item in included if item["wave_number"] is not None
            }
            plan_readiness = [
                item
                for item in client.wave_readiness(
                    collector_id, observed_after.isoformat(), observed_before.isoformat()
                )["waves"]
                if int(item["wave"]) in plan_waves
            ]
        plan_pdf = build_migration_plan_pdf(
            selected_collector, plan, plan_readiness, plan_dependencies, drift
        )
        st.download_button(
            "Plan package (PDF)",
            data=plan_pdf,
            file_name=plan_report_filename(selected_collector["display_name"], plan, "pdf"),
            mime="application/pdf",
            key=f"plan_pdf_{plan['plan_id']}",
            type="primary",
            icon="⬇️",
            width="stretch",
            help="Download the selected migration-plan version as a PDF package.",
        )
    except (DashboardClientError, KeyError, TypeError, ValueError):
        st.warning("The selected plan PDF package could not be generated.")

    try:
        plan_connections = client.detailed_connections(
            collector_id, observed_after.isoformat(), observed_before.isoformat()
        )
        plan_excel = build_migration_plan_excel(
            selected_collector,
            plan,
            plan_readiness,
            plan_dependencies,
            drift,
            plan_connections,
        )
        st.download_button(
            "Plan workbook (Excel)",
            data=plan_excel,
            file_name=plan_report_filename(selected_collector["display_name"], plan, "xlsx"),
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"plan_excel_{plan['plan_id']}",
            type="primary",
            icon="⬇️",
            width="stretch",
            help="Download the selected plan and its technical evidence as Excel.",
        )
    except (DashboardClientError, KeyError, TypeError, ValueError):
        st.warning(
            "The plan workbook could not be generated. The PDF package remains available; "
            "check the detailed-connections API before retrying the workbook."
        )

    if plan["status"] == "draft":
        with st.expander("Change VM assignment", expanded=True):
            assignment_options = {
                f"{item['vm_name']} ({item['vm_uuid']})": item for item in assignments
            }
            selected_assignment_labels = st.multiselect(
                "VMs to change",
                assignment_options,
                key=f"plan_vms_{plan['plan_id']}",
            )
            selected_assignments = [
                assignment_options[label] for label in selected_assignment_labels
            ]
            recommended_waves = sorted(
                {
                    int(item["recommended_wave_number"])
                    for item in assignments
                    if item.get("recommended_wave_number") is not None
                }
            )
            planned_waves = sorted(
                {int(item["wave_number"]) for item in included if item["wave_number"] is not None}
            )
            custom_waves = [wave for wave in planned_waves if wave not in recommended_waves]
            actions = [
                "Move this VM to a recommended wave",
                "Create a new planner wave for this VM",
            ]
            if custom_waves:
                actions.append("Move this VM to an existing planner-created wave")
            actions.append("Exclude this VM from this plan")
            action = st.radio("Planner action", actions, key=f"plan_action_{plan['plan_id']}")
            target_wave_number: int | None
            if action == "Move this VM to a recommended wave":
                recommended_options = {
                    f"Recommended Wave {wave}": wave for wave in recommended_waves
                }
                selected_target = st.selectbox(
                    "Recommended wave",
                    recommended_options,
                    key=f"recommended_wave_{plan['plan_id']}",
                )
                target_wave_number = recommended_options[selected_target]
            elif action == "Move this VM to an existing planner-created wave":
                custom_options = {f"Planner Wave {wave}": wave for wave in custom_waves}
                selected_target = st.selectbox(
                    "Planner-created wave", custom_options, key=f"custom_wave_{plan['plan_id']}"
                )
                target_wave_number = custom_options[selected_target]
            elif action == "Create a new planner wave for this VM":
                target_wave_number = max(planned_waves, default=0) + 1
                st.info(
                    f"This creates Planner Wave {target_wave_number} containing the selected VMs."
                )
            else:
                target_wave_number = None
                st.info(
                    "This excludes only the selected VMs from this plan; it does not exclude "
                    "a whole wave."
                )
            with st.form(f"edit_plan_assignment_{plan['plan_id']}"):
                assignment_note = st.text_area(
                    "Assignment note (optional)",
                    value=(selected_assignments[0].get("note") or "")
                    if len(selected_assignments) == 1
                    else "",
                )
                save_assignment = st.form_submit_button("Save VM assignment", type="primary")
            if save_assignment:
                if not selected_assignments:
                    st.error("Select at least one VM.")
                else:
                    try:
                        client.update_migration_plan_assignments(
                            collector_id,
                            str(plan["plan_id"]),
                            {
                                "vm_uuids": [item["vm_uuid"] for item in selected_assignments],
                                "disposition": "included"
                                if target_wave_number is not None
                                else "excluded",
                                "wave_number": target_wave_number,
                                "note": assignment_note,
                            },
                        )
                        st.success(f"Updated {len(selected_assignments)} VM assignment(s).")
                        st.rerun()
                    except DashboardClientError as error:
                        st.error(str(error))
        with st.expander("Approve this plan"):
            st.warning("Approval locks this version and supersedes the currently approved plan.")
            with st.form(f"approve_plan_{plan['plan_id']}"):
                approver_name = st.text_input("Approver name")
                approval_note = st.text_area("Approval note (optional)")
                confirmation = st.text_input("Type APPROVE to confirm")
                approve = st.form_submit_button("Approve migration plan", type="primary")
            if approve:
                if confirmation != "APPROVE":
                    st.error("Type APPROVE exactly to confirm this irreversible approval action.")
                else:
                    try:
                        client.approve_migration_plan(
                            collector_id,
                            str(plan["plan_id"]),
                            {"planner_name": approver_name, "note": approval_note},
                        )
                        st.success(
                            "Migration plan approved. Readiness and exports now use this version."
                        )
                        st.rerun()
                    except DashboardClientError as error:
                        st.error(str(error))
    assignment_page_key = f"migration_plan_assignment_page_{plan['plan_id']}"
    st.session_state.setdefault(assignment_page_key, 1)
    assignment_page_size = 25
    assignment_page_count = max(
        1, (len(assignments) + assignment_page_size - 1) // assignment_page_size
    )
    assignment_page = min(st.session_state[assignment_page_key], assignment_page_count)
    st.session_state[assignment_page_key] = assignment_page
    start = (assignment_page - 1) * assignment_page_size
    st.dataframe(
        [
            {
                "VM": item["vm_name"],
                "Disposition": item["disposition"],
                "Wave": str(item["wave_number"]) if item["wave_number"] is not None else "Excluded",
                "Planner note": item.get("note") or "",
            }
            for item in assignments[start : start + assignment_page_size]
        ],
        width="stretch",
        hide_index=True,
    )
    if assignment_page_count > 1:
        previous, page_label, next_page = st.columns((1, 3, 1))
        with previous:
            if st.button("Previous", key=f"previous_{assignment_page_key}"):
                st.session_state[assignment_page_key] = max(1, assignment_page - 1)
                st.rerun()
        with page_label:
            st.caption(
                f"Page {assignment_page} of {assignment_page_count}; "
                f"showing up to {assignment_page_size} VMs per page."
            )
        with next_page:
            if st.button("Next", key=f"next_{assignment_page_key}"):
                st.session_state[assignment_page_key] = min(
                    assignment_page_count, assignment_page + 1
                )
                st.rerun()


def render_dependency_review(collectors: list[dict[str, object]]) -> None:
    """Show dependencies that inform cutover but never automatically join waves."""

    st.subheader("Dependency Review")
    st.write(
        "Review external endpoints, shared infrastructure, and unavailable internal VMs "
        "before approving a migration wave. These items do not automatically join waves."
    )
    if not collectors:
        st.info("No registered collectors are available yet.")
        return
    options = {
        f"{item['display_name']} - {item['tenant_id']} - {item['collector_id']}": item[
            "collector_id"
        ]
        for item in collectors
    }
    selected_label = _remembered_selectbox("Collector", options, "dependency_collector")
    observed_after, observed_before, range_label = _observation_window()
    try:
        report = client.dependencies(
            options[selected_label], observed_after.isoformat(), observed_before.isoformat()
        )
        waves = client.waves(
            options[selected_label],
            observed_after.isoformat(),
            observed_before.isoformat(),
            use_approved_plan=False,
        )
    except DashboardClientError as error:
        st.error(str(error))
        return
    st.caption(f"Reporting window: {range_label}.")
    items = list(report["items"])
    wave_options = {"All dependencies": None}
    wave_options.update(
        {
            f"Wave {wave['wave']} - {len(wave['server_names'])} VM(s)": index
            for index, wave in enumerate(waves)
        }
    )
    selected_wave_label = _remembered_selectbox("Migration wave", wave_options, "dependency_wave")
    selected_wave_index = wave_options[selected_wave_label]
    if selected_wave_index is not None:
        selected_vm_uuids = set(waves[selected_wave_index]["server_vm_uuids"])
        items = [
            item
            for item in items
            if item.get("source_vm_uuid") in selected_vm_uuids
            or item.get("destination_vm_uuid") in selected_vm_uuids
        ]
    active_vm_uuids = (
        selected_vm_uuids
        if selected_wave_index is not None
        else {vm_uuid for wave in waves for vm_uuid in wave["server_vm_uuids"]}
    )
    reviewed_vm_uuids = {
        item["source_vm_uuid"] for item in items if item.get("source_vm_uuid") in active_vm_uuids
    }
    external_endpoints = {
        item["destination_ip"] for item in items if item.get("category") == "External dependency"
    }
    shared_services = {
        (item["destination_ip"], item["classification"])
        for item in items
        if str(item.get("category", "")).startswith("Shared infrastructure")
    }
    unavailable_vms = {
        item["unavailable_vm_uuid"]
        for item in items
        if item.get("category") == "Unavailable internal VM" and item.get("unavailable_vm_uuid")
    }
    not_reviewed_count = _not_reviewed_dependency_count(items)
    cards = st.columns(6)
    card_values = (
        (
            "Wave members" if selected_wave_index is not None else "Active inventory VMs",
            len(active_vm_uuids),
        ),
        ("Not reviewed", not_reviewed_count),
        ("VMs with review dependencies", len(reviewed_vm_uuids)),
        ("External endpoints", len(external_endpoints)),
        ("Shared services", len(shared_services)),
        ("Unavailable internal VMs", len(unavailable_vms)),
    )
    for column, (label, value) in zip(cards, card_values, strict=True):
        with column:
            card(label, value, warning=label == "Not reviewed" and value > 0)
    st.caption(
        f"Review scope: {len(active_vm_uuids)} active wave member(s); "
        f"{len(reviewed_vm_uuids)} VM(s) have review dependencies."
    )
    categories = sorted({str(item.get("category", "")) for item in items})
    selected_categories = st.multiselect("Categories", categories, default=categories)
    search = st.text_input("Filter by source, destination, or IP").casefold().strip()
    filtered_items = [
        item
        for item in items
        if str(item.get("category")) in selected_categories
        and (
            not search
            or search
            in " ".join(
                str(item.get(field, "")) for field in ("source_vm", "destination", "destination_ip")
            ).casefold()
        )
    ]
    if not filtered_items:
        st.info("No review dependencies were found for the selected filters and time range.")
        return
    readiness_status_filter = st.selectbox(
        "Readiness status filter",
        ("All", "Action required", "Investigate", "High impact", "Advisory", "Resolved"),
        key="dependency_readiness_status_filter",
    )
    if readiness_status_filter != "All":
        filtered_items = [
            item
            for item in filtered_items
            if dependency_readiness_status(
                str(item.get("planner_decision") or "") or None,
                str(item.get("category") or ""),
            )
            == readiness_status_filter
        ]
    review_filter = st.selectbox(
        "Planner review filter",
        ("Not reviewed", "Reviewed", "All"),
        key="dependency_planner_review_filter",
    )
    decision_items = [
        item
        for item in filtered_items
        if review_filter == "All"
        or (review_filter == "Not reviewed" and not item.get("planner_decision"))
        or (review_filter == "Reviewed" and item.get("planner_decision"))
    ]
    decision_options = {
        (
            f"{item['category']} | {item['source_vm']} → "
            f"{item['destination']} ({item['destination_ip']}) | {item['port_service']}"
        ): item
        for item in decision_items
    }
    with st.expander("Record planner decision"):
        if not decision_options:
            st.info(f"No {review_filter.casefold()} dependencies match the selected filters.")
        else:
            selected_decision_labels = st.multiselect(
                "Dependencies",
                decision_options,
                max_selections=250,
                help="Select one or more dependencies when they need the same decision and note.",
            )
            selected_dependencies = [decision_options[label] for label in selected_decision_labels]
            selected_categories = {item["category"] for item in selected_dependencies}
            initial_decision_options = (
                _planner_decisions(selected_dependencies[0]["category"])
                if selected_dependencies
                else ()
            )
            common_decisions = (
                tuple(
                    decision
                    for decision in initial_decision_options
                    if all(
                        decision in _planner_decisions(category) for category in selected_categories
                    )
                )
                if selected_categories
                else ()
            )
            with st.form("dependency_decision"):
                decision = st.selectbox(
                    "Planner decision",
                    common_decisions,
                    disabled=not common_decisions,
                )
                note = st.text_area(
                    "Planner note",
                    value=(
                        selected_dependencies[0].get("planner_note") or ""
                        if len(selected_dependencies) == 1
                        else ""
                    ),
                )
                if decision in {"Confirmed hard dependency", "Not relevant / excluded"}:
                    st.caption("A planner note is required for this decision.")
                save = st.form_submit_button(
                    "Save decision" if len(selected_dependencies) == 1 else "Save decisions",
                    type="primary",
                    disabled=not selected_dependencies or not common_decisions,
                )
            if save:
                try:
                    response = client.save_dependency_decisions(
                        options[selected_label],
                        {
                            "dependencies": [
                                {
                                    key: str(item[key])
                                    for key in (
                                        "source_vm_uuid",
                                        "destination_identity",
                                        "protocol",
                                        "port_key",
                                        "category",
                                    )
                                }
                                for item in selected_dependencies
                            ],
                            "decision": decision,
                            "note": note,
                        },
                    )
                    count = response["count"]
                    noun = "dependency" if count == 1 else "dependencies"
                    st.success(f"Planner decision saved for {count} {noun}.")
                    st.rerun()
                except DashboardClientError as error:
                    st.error(str(error))
    st.dataframe(
        [
            {
                "Category": item.get("category"),
                "Source VM": item.get("source_vm"),
                "Destination": item.get("destination"),
                "Destination IP": item.get("destination_ip"),
                "Protocol": item.get("protocol"),
                "Port/service": item.get("port_service"),
                "Observations": item.get("observation_count"),
                "Planner decision": item.get("planner_decision") or "Not reviewed",
                "Readiness status": dependency_readiness_status(
                    str(item.get("planner_decision") or "") or None,
                    str(item.get("category") or ""),
                ),
                "Planner note": item.get("planner_note") or "",
                "Review reason": item.get("reason"),
            }
            for item in decision_items
        ],
        width="stretch",
        hide_index=True,
    )


if st.query_params.get("page") == "migration-plan":
    st.session_state["dashboard_page"] = "Migration Plan"

with st.sidebar:
    st.header("Control Plane")
    st.caption("OCI-hosted collector management and dependency reporting.")
    page = st.radio(
        "Navigation",
        _DASHBOARD_NAVIGATION_PAGES,
        label_visibility="collapsed",
        key="dashboard_page",
    )

st.title("Migration Discovery Dashboard")
st.caption(
    "Customer-side collectors report inventory and dependency telemetry to this OCI service."
)

try:
    settings = ControlPlaneSettings()
    settings.validate_runtime()
    client = ControlPlaneDashboardClient(
        settings.control_plane_dashboard_api_url,
        settings.dashboard_api_key().get_secret_value(),
        settings.control_plane_admin_api_key.get_secret_value(),
    )
except ValueError as error:
    st.error(f"Dashboard deployment configuration is incomplete: {error}")
    st.stop()

collectors: list[dict[str, object]] = []

if page == "Enroll Agent Collector":
    st.subheader("Enroll an agent collector appliance")
    st.warning(
        "Administrator-only operation. The enrollment code is displayed once; copy it directly "
        "into the customer-side collector setup page."
    )
    enrollment_mode = st.radio(
        "Operation", ("New collector", "Reconnect existing collector"), horizontal=True
    )
    if enrollment_mode == "New collector":
        with st.form("create-enrollment"):
            tenant_id = st.text_input("Customer tenant ID")
            expires_in_minutes = st.selectbox("Code expiry", (15, 30, 60), index=0)
            create = st.form_submit_button("Generate one-time enrollment code", type="primary")
        if create:
            try:
                enrollment = client.create_enrollment(tenant_id, expires_in_minutes)
                st.success(f"Code expires at {enrollment['expires_at']}.")
                st.code(enrollment["enrollment_code"], language=None)
                st.caption(
                    "Copy this code now. It cannot be retrieved again after this page reruns."
                )
            except DashboardClientError as error:
                st.error(str(error))
    else:
        try:
            reconnect_collectors = client.collectors()
        except DashboardClientError as error:
            st.error(str(error))
            st.stop()
        if not reconnect_collectors:
            st.info("No existing collectors are available to reconnect.")
            st.stop()
        collector_options = {
            f"{item['display_name']} · {item['tenant_id']} · {item['collector_id']}": item[
                "collector_id"
            ]
            for item in reconnect_collectors
        }
        with st.form("create-reconnection-code"):
            selected_collector = st.selectbox("Existing collector", collector_options)
            expires_in_minutes = st.selectbox("Code expiry", (15, 30, 60), index=0)
            create = st.form_submit_button("Generate one-time reconnection code", type="primary")
        if create:
            try:
                reconnection = client.create_reconnection_code(
                    collector_options[selected_collector], expires_in_minutes
                )
                st.success(f"Code expires at {reconnection['expires_at']}.")
                st.code(reconnection["reconnection_code"], language=None)
                st.caption(
                    "Copy this code now. It cannot be retrieved again after this page reruns."
                )
            except DashboardClientError as error:
                st.error(str(error))
else:
    try:
        collectors = client.collectors()
    except DashboardClientError as error:
        st.error(str(error))
        st.stop()

if page == "Current Environment":
    render_current_environment(collectors)
elif page == "Agent Collector's Status":
    st.subheader("Agent Collector's Status")
    online = sum(item["status"] == "online" for item in collectors)
    left, middle, right = st.columns(3)
    with left:
        card("Registered agent collectors", len(collectors))
    with middle:
        card("Online agent collectors", online)
    with right:
        card("Agent collectors needing attention", len(collectors) - online)
    st.divider()
    st.dataframe(collectors, width="stretch", hide_index=True)
    if collectors:
        with st.expander("Delete collector agent and all retained data"):
            st.warning(
                "This permanently deletes the selected collector, its VM inventory, observed "
                "connections, migration-wave data, and reconnection codes. This cannot be undone."
            )
            collector_options = {
                f"{item['display_name']} · {item['tenant_id']} · {item['collector_id']}": item[
                    "collector_id"
                ]
                for item in collectors
            }
            selected_for_deletion = st.selectbox(
                "Collector agent to delete", collector_options, key="delete_collector_agent"
            )
            deletion_confirmation = st.text_input(
                "Type DELETE to confirm permanent removal", key="delete_collector_confirmation"
            )
            if st.button(
                "Permanently delete collector agent",
                type="secondary",
                disabled=deletion_confirmation != "DELETE",
            ):
                try:
                    deleted = client.delete_collector(collector_options[selected_for_deletion])
                    st.success(
                        f"Collector {deleted['collector_id']} and its retained data were deleted."
                    )
                    st.rerun()
                except DashboardClientError as error:
                    st.error(str(error))
elif page == "VM/Server Inventory":
    st.subheader("VM/Server Inventory")
    if not collectors:
        st.info("No registered collectors are available yet.")
        st.stop()
    options = {
        f"{item['display_name']} · {item['tenant_id']}": item["collector_id"] for item in collectors
    }
    selected = st.selectbox("Collector", options)
    st.caption(f"Selected Collector ID: {options[selected]}")
    try:
        inventory = client.inventory(options[selected])
    except DashboardClientError as error:
        st.error(str(error))
        st.stop()
    first, second = st.columns(2)
    with first:
        card("Active discovered VMs", len(inventory))
    with second:
        card("Powered-on VMs", sum(item["power_state"] == "poweredOn" for item in inventory))
    st.divider()
    if inventory:
        st.dataframe(
            [
                {
                    "VM name": item["vm_name"],
                    "Hostname": item["hostname"],
                    "IP addresses": item["ips"],
                    "Power state": item["power_state"],
                    "Cluster": item["cluster"],
                    "Folder": item["folder"],
                    "Operating system": item["os_name"],
                }
                for item in inventory
            ],
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("This collector has not uploaded an inventory snapshot yet.")
elif page == "Migration Waves":
    render_migration_waves(collectors)
elif page == "Dependency Review":
    render_dependency_review(collectors)
elif page == "Migration Plan":
    render_migration_plan(collectors)
