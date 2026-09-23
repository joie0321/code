"""OCI-facing Streamlit dashboard for registered VMware collector agents."""

from __future__ import annotations

import html
from datetime import UTC, datetime, time, timedelta

import streamlit as st

try:  # Supports `streamlit run control_plane/dashboard.py` from the project root.
    from control_plane.config import ControlPlaneSettings
    from control_plane.dashboard_client import ControlPlaneDashboardClient, DashboardClientError
except ModuleNotFoundError:  # pragma: no cover - Streamlit executes the file as a script.
    from config import ControlPlaneSettings
    from dashboard_client import ControlPlaneDashboardClient, DashboardClientError

st.set_page_config(page_title="VMware Migration Control Plane", page_icon="🧭", layout="wide")
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
      font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)


def card(label: str, value: int) -> None:
    st.markdown(
        "<div class='summary-card'><div class='summary-card__accent'></div>"
        f"<div class='summary-card__label'>{html.escape(label)}</div>"
        f"<div class='summary-card__value'>{value:,}</div></div>",
        unsafe_allow_html=True,
    )


def _escape_dot(value: object) -> str:
    """Encode untrusted inventory values for a Graphviz quoted string."""

    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


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


def _observation_window() -> tuple[datetime, datetime, str]:
    """Return an inclusive UTC reporting window selected by the operator."""

    range_name = st.selectbox(
        "Observed connection time range",
        ("1 day", "5 days", "1 week", "1 month", "Custom time range"),
        key="wave_time_range",
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
        key="wave_custom_dates",
    )
    start = datetime.combine(start_date, time.min, tzinfo=UTC)
    end = datetime.combine(end_date, time.max, tzinfo=UTC)
    if end < start:
        st.error("The custom end date must be on or after the start date.")
        st.stop()
    return start, end, f"{start_date.isoformat()} to {end_date.isoformat()}"


def _wave_graph(wave: dict[str, object], connections: list[dict[str, object]]) -> str:
    """Create a bounded DOT graph for one wave without trusting inventory labels."""

    vm_uuids = list(wave["server_vm_uuids"])
    vm_names = list(wave["server_names"])
    names_by_uuid = dict(zip(vm_uuids, vm_names, strict=True))
    node_ids = {vm_uuid: f"vm_{index}" for index, vm_uuid in enumerate(vm_uuids, start=1)}
    edge_ports: dict[tuple[str, str], set[str]] = {}
    external_ids: dict[str, str] = {}

    for connection in connections:
        source_uuid = connection.get("source_vm_uuid")
        if source_uuid not in node_ids:
            continue
        destination_uuid = connection.get("destination_vm_uuid")
        if destination_uuid in node_ids:
            destination_id = node_ids[destination_uuid]
        else:
            destination_ip = str(connection.get("destination_ip", "unknown"))
            destination_id = external_ids.setdefault(
                destination_ip, f"external_{len(external_ids) + 1}"
            )
        edge = (node_ids[source_uuid], destination_id)
        edge_ports.setdefault(edge, set()).add(
            f"{connection.get('protocol', 'tcp')}/{connection.get('destination_port', '?')}"
        )
        if len(edge_ports) >= 100:
            break

    lines = [
        "digraph migration_wave {",
        "rankdir=LR;",
        'graph [bgcolor="transparent", pad="0.18", nodesep="0.45", ranksep="0.7"];',
        (
            'node [shape=box, style="rounded,filled", fontname="Arial", fontsize=10, '
            'margin="0.12,0.06", color="#396a9f", fillcolor="#17324d", '
            'fontcolor="#f5f8ff"];'
        ),
        'edge [fontname="Arial", fontsize=8, color="#70a6d8", fontcolor="#c8dbef"];',
    ]
    for vm_uuid, node_id in node_ids.items():
        lines.append(f'{node_id} [label="{_escape_dot(names_by_uuid[vm_uuid])}"];')
    for destination_ip, node_id in external_ids.items():
        lines.append(
            f'{node_id} [label="External\\n{_escape_dot(destination_ip)}", '
            'color="#a47527", fillcolor="#493719"];'
        )
    for (source_id, destination_id), ports in edge_ports.items():
        label = ", ".join(sorted(ports))
        lines.append(f'{source_id} -> {destination_id} [label="{_escape_dot(label)}"];')
    lines.append("}")
    return "\n".join(lines)


def render_migration_waves(collectors: list[dict[str, object]]) -> None:
    """Render one collector's reporting window without resetting dashboard choices."""

    st.subheader("Migration waves")
    st.write(
        "Review TCP connections uploaded by one collector. Powered-off VMs are excluded from "
        "wave assignment; external destinations are shown only as dependencies."
    )
    if not collectors:
        st.info("No registered collectors are available yet.")
        return
    controls, refresh_control = st.columns((4, 1))
    with controls:
        st.caption("Use Refresh now to load the latest inventory and connection data.")
    with refresh_control:
        st.button("Refresh now", key="refresh_migration_waves", use_container_width=True)
    options = {
        f"{item['display_name']} - {item['tenant_id']}": item["collector_id"] for item in collectors
    }
    selected_label = _remembered_selectbox("Collector", options, "wave_collector")
    observed_after, observed_before, range_label = _observation_window()
    selected_collector_id = options[selected_label]
    try:
        summary = client.wave_summary(
            selected_collector_id, observed_after.isoformat(), observed_before.isoformat()
        )
        waves = client.waves(
            selected_collector_id, observed_after.isoformat(), observed_before.isoformat()
        )
        connections = client.connections(
            selected_collector_id, observed_after.isoformat(), observed_before.isoformat()
        )
    except DashboardClientError as error:
        st.error(str(error))
        return

    st.caption(f"Reporting window: {range_label}. Graphs include at most 100 unique edges.")
    cards = st.columns(5)
    with cards[0]:
        card("Migration waves", summary["wave_count"])
    with cards[1]:
        card("Active inventory VMs", summary["active_inventory_vm_count"])
    with cards[2]:
        card("Eligible powered-on VMs", summary["eligible_vm_count"])
    with cards[3]:
        card("Waves with connections", summary["waves_with_observed_connections"])
    with cards[4]:
        card("VMs without connections", summary["vms_without_observed_connections"])

    if not waves:
        st.info("No eligible powered-on VMs are available in this collector inventory.")
        return
    wave_options = {
        f"Wave {wave['wave']} - {len(wave['server_names'])} VM(s)": index
        for index, wave in enumerate(waves)
    }
    selected_wave_label = _remembered_selectbox("Migration wave", wave_options, "selected_wave")
    selected_wave = waves[wave_options[selected_wave_label]]
    st.divider()
    st.markdown(f"#### {html.escape(selected_wave_label)}")
    st.caption(str(selected_wave["reason"]))
    st.graphviz_chart(_wave_graph(selected_wave, connections), use_container_width=False)

    wave_vm_uuids = set(selected_wave["server_vm_uuids"])
    wave_connections = [
        connection
        for connection in connections
        if connection.get("source_vm_uuid") in wave_vm_uuids
    ]
    with st.expander(f"Observed connections for {selected_wave_label} ({len(wave_connections)})"):
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
            st.dataframe(display_connections, use_container_width=True, hide_index=True)
        else:
            st.info("No observations were recorded for this wave in the selected time range.")


with st.sidebar:
    st.header("Control Plane")
    st.caption("OCI-hosted collector management and dependency reporting.")
    page = st.radio(
        "Navigation",
        ("Collectors", "VM Inventory", "Migration Waves", "Enroll Collector"),
        label_visibility="collapsed",
    )

st.title("VMware Migration Control Plane")
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
if page == "Enroll Collector":
    st.subheader("Enroll a collector appliance")
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

if page == "Collectors":
    st.subheader("Registered collectors")
    online = sum(item["status"] == "online" for item in collectors)
    left, middle, right = st.columns(3)
    with left:
        card("Registered collectors", len(collectors))
    with middle:
        card("Online collectors", online)
    with right:
        card("Collectors needing attention", len(collectors) - online)
    st.divider()
    st.dataframe(collectors, use_container_width=True, hide_index=True)
elif page == "VM Inventory":
    st.subheader("Collector VM inventory")
    if not collectors:
        st.info("No registered collectors are available yet.")
        st.stop()
    options = {
        f"{item['display_name']} · {item['tenant_id']}": item["collector_id"] for item in collectors
    }
    selected = st.selectbox("Collector", options)
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
        st.dataframe(inventory, use_container_width=True, hide_index=True)
    else:
        st.info("This collector has not uploaded an inventory snapshot yet.")
elif page == "Migration Waves":
    render_migration_waves(collectors)
