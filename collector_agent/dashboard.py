"""Local Streamlit setup and health dashboard for a collector appliance."""

from __future__ import annotations

import html

import streamlit as st

try:  # Supports `streamlit run collector_agent/dashboard.py`.
    from collector_agent.dashboard_client import AgentDashboardClient, AgentDashboardClientError
except ModuleNotFoundError:  # pragma: no cover - Streamlit executes this as a script.
    from dashboard_client import AgentDashboardClient, AgentDashboardClientError

st.set_page_config(page_title="Migration Collector", page_icon="M", layout="wide")
st.markdown(
    """
    <style>
    .agent-card { min-height: 102px; padding: 1rem; border: 1px solid rgba(255,255,255,.12);
      border-radius: 16px; background: linear-gradient(145deg, #263244, #141b27);
      box-shadow: 0 10px 24px rgba(0,0,0,.20), inset 0 1px 0 rgba(255,255,255,.08); }
    .agent-card__label { color: #B7C5D6; font-size: .78rem; font-weight: 600; }
    .agent-card__value { margin-top: .45rem; color: #F7FAFC; font-size: 1.35rem; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)


def card(label: str, value: str) -> None:
    st.markdown(
        "<div class='agent-card'>"
        f"<div class='agent-card__label'>{html.escape(label)}</div>"
        f"<div class='agent-card__value'>{html.escape(value)}</div></div>",
        unsafe_allow_html=True,
    )


client = AgentDashboardClient()


def source_options(status: dict[str, object]) -> dict[str, str]:
    sources = status.get("sources", [])
    if not isinstance(sources, list):
        return {}
    return {
        f"{item['display_name']} — {item['collector_id']}": item["collector_id"]
        for item in sources
        if isinstance(item, dict)
    }


@st.fragment(run_every="15s")
def render_agent_status() -> None:
    """Refresh local source health without contacting vCenter."""

    try:
        status = client.status()
    except AgentDashboardClientError as error:
        st.warning(str(error))
        return
    sources = status["sources"]
    st.caption("Status refreshes automatically every 15 seconds.")
    if st.button("Refresh now", key="refresh_agent_status"):
        st.rerun()
    left, middle, right = st.columns(3)
    with left:
        card("Registration", str(status["registration"]).replace("_", " ").title())
    with middle:
        card("Registered sources", str(len(sources)))
    with right:
        card("IPFIX exporters", str(sum(item["ipfix_exporter_count"] for item in sources)))
    if sources:
        st.dataframe(
            sources,
            column_config={
                "display_name": "Source",
                "collector_id": "Collector ID",
                "vcenter_configured": "vCenter configured",
                "guest_credentials_configured": "Guest credentials configured",
                "last_inventory_vm_count": "Inventory VMs",
                "last_inventory_sync_at": "Last inventory sync",
                "last_heartbeat_at": "Last heartbeat",
                "ipfix_exporter_count": "IPFIX exporters",
                "ipfix_status": "IPFIX status",
                "ipfix_detail": "IPFIX detail",
                "last_error": "Last error",
            },
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("Register a collection source to begin VMware setup.")
    rejected_datagrams = status.get("unassigned_ipfix_datagrams", [])
    if rejected_datagrams:
        st.warning(
            "Unassigned IPFIX datagrams are reaching this appliance. Assign the exporter IP "
            "to the correct source in VMware Setup."
        )
        st.dataframe(
            rejected_datagrams,
            column_config={
                "exporter_ip": "Rejected exporter IP",
                "datagrams_rejected": "Rejected datagrams",
                "last_rejected_at": "Last seen",
                "reason": "Reason",
            },
            column_order=("exporter_ip", "datagrams_rejected", "last_rejected_at", "reason"),
            use_container_width=True,
            hide_index=True,
        )


@st.fragment(run_every="15s")
def render_ipfix_status(collector_id: str) -> None:
    """Refresh IPFIX counters that belong only to the selected source."""

    try:
        ipfix_setup = client.ipfix_setup(collector_id)
        listener = ipfix_setup["listener"]
    except AgentDashboardClientError as error:
        st.warning(str(error))
        return
    st.caption("IPFIX counters refresh automatically every 15 seconds.")
    if st.button("Refresh now", key=f"refresh_ipfix_status_{collector_id}"):
        st.rerun()
    (st.success if listener["status"] == "running" else st.info)(
        "IPFIX listener is running."
        if listener["status"] == "running"
        else "IPFIX listener is stopped."
    )
    columns = st.columns(4)
    columns[0].metric("Datagrams accepted", listener["datagrams_received"])
    columns[1].metric("Selected exporters", len(listener["selected_exporters"]))
    columns[2].metric("Flows found", listener["flows_received"])
    columns[3].metric("Observations uploaded", listener["observations_stored"])
    if listener["last_received_at"]:
        st.caption(f"Last datagram for this source: {listener['last_received_at']}")
    if ipfix_setup.get("last_error"):
        st.warning(str(ipfix_setup["last_error"]))


with st.sidebar:
    st.header("Collector Appliance")
    st.caption("Local setup only. Customer credentials remain on this appliance.")
    page = st.radio(
        "Navigation",
        ("Collector Agent Setup", "VMware Setup", "Agent Status"),
        label_visibility="collapsed",
    )

st.title("Migration Collector Agent")
st.caption("Register collection sources, configure VMware collection, and monitor local health.")

try:
    current_status = client.status()
except AgentDashboardClientError as error:
    st.error(str(error))
    st.info("Start the local collector-agent API before opening this dashboard.")
    st.stop()

if page == "Collector Agent Setup":
    st.subheader("Register or reconnect a collection source")
    st.write("Create the one-time enrollment or reconnection code from the OCI control plane.")
    with st.form("registration"):
        control_plane_url = st.text_input("OCI control-plane URL", value="http://127.0.0.1:8100")
        tenant_id = st.text_input("Customer tenant ID", value="lab-customer")
        enrollment_code = st.text_input("One-time enrollment code", type="password")
        display_name = st.text_input("Source name", value="vmware-vcenter-01")
        register = st.form_submit_button("Register source")
    if register:
        try:
            result = client.register(
                {
                    "control_plane_url": control_plane_url,
                    "tenant_id": tenant_id,
                    "enrollment_code": enrollment_code,
                    "display_name": display_name,
                }
            )
            st.success(f"Source registered: {result['collector_id']}.")
        except AgentDashboardClientError as error:
            st.error(str(error))

    with st.expander("Reconnect an existing source after an appliance restart"):
        st.caption("Reconnection rotates the token while preserving the source history.")
        with st.form("reconnection"):
            reconnect_url = st.text_input("OCI control-plane URL", value="http://127.0.0.1:8100")
            collector_id = st.text_input("Existing collector ID")
            reconnect_name = st.text_input("Source name (optional)")
            reconnection_code = st.text_input("One-time reconnection code", type="password")
            reconnect = st.form_submit_button("Reconnect source")
        if reconnect:
            try:
                result = client.reconnect(
                    {
                        "control_plane_url": reconnect_url,
                        "collector_id": collector_id,
                        "reconnection_code": reconnection_code,
                        "display_name": reconnect_name or None,
                    }
                )
                st.success(f"Source reconnected: {result['collector_id']}.")
            except AgentDashboardClientError as error:
                st.error(str(error))
    if current_status["sources"]:
        st.dataframe(current_status["sources"], use_container_width=True, hide_index=True)

elif page == "VMware Setup":
    st.subheader("VMware setup")
    options = source_options(current_status)
    if not options:
        st.info("Register or reconnect a collection source before configuring VMware.")
        st.stop()
    selected_label = st.selectbox("Collection source", list(options))
    selected_collector_id = options[selected_label]
    st.caption("Credentials are retained only in memory for the selected source.")
    selected_source = next(
        item for item in current_status["sources"] if item["collector_id"] == selected_collector_id
    )
    st.info(
        "Selected source inventory in local agent: "
        f"{selected_source['last_inventory_vm_count']} VM(s). "
        f"Collector ID: {selected_collector_id}"
    )
    with st.form("vcenter"):
        host = st.text_input("vCenter FQDN or IP")
        username = st.text_input("vCenter username")
        password = st.text_input("vCenter password", type="password")
        verify_tls = st.checkbox("Verify vCenter TLS certificate", value=True)
        configure = st.form_submit_button("Store credentials in memory")
    if configure:
        try:
            client.configure_credentials(
                {
                    "collector_id": selected_collector_id,
                    "vcenter_host": host,
                    "vcenter_username": username,
                    "vcenter_password": password,
                    "verify_tls": verify_tls,
                }
            )
            st.success("vCenter credentials are held in collector memory for this source.")
        except AgentDashboardClientError as error:
            st.error(str(error))

    st.subheader("Synchronize inventory")
    if st.button("Sync vCenter inventory", type="primary"):
        try:
            result = client.sync_inventory(selected_collector_id)
            st.success(
                f"Inventory synchronized for Collector ID {selected_collector_id}: "
                f"{result['discovered']} discovered, {result['accepted']} accepted "
                "by the control plane."
            )
        except AgentDashboardClientError as error:
            st.error(str(error))

    st.divider()
    st.subheader("IPFIX setup")
    st.caption(
        "One appliance listener uses UDP 4739 and routes each exporter to its assigned source."
    )
    try:
        ipfix_setup = client.ipfix_setup(selected_collector_id)
    except AgentDashboardClientError as error:
        st.error(str(error))
        st.stop()
    render_ipfix_status(selected_collector_id)
    candidates = ipfix_setup["exporter_candidates"]
    source_ipfix_running = bool(ipfix_setup["selected_exporters"])
    if candidates:
        clusters = sorted({item["cluster"] or "Unclustered" for item in candidates})
        selected_clusters = st.multiselect("Clusters to trust", clusters, default=clusters)
        included = [
            item for item in candidates if (item["cluster"] or "Unclustered") in selected_clusters
        ]
        excluded_exporters: list[str] = []
        for index, item in enumerate(included):
            include = st.checkbox(
                f"Include {item['host_name']} ({item['cluster'] or 'Unclustered'}) — "
                f"{', '.join(item['vmk0_ips'])}",
                value=True,
                key=f"ipfix_{selected_collector_id}_{index}_{item['host_name']}",
            )
            if not include:
                excluded_exporters.extend(item["vmk0_ips"])
    else:
        selected_clusters, excluded_exporters = [], []
        st.warning("No vmk0 candidates are available. Synchronize inventory or enter exporter IPs.")
    manual_exporters = st.text_area("Additional exporter IPs (one address per line)")
    if source_ipfix_running:
        if st.button("Stop IPFIX for this source"):
            try:
                client.stop_ipfix(selected_collector_id)
                st.rerun()
            except AgentDashboardClientError as error:
                st.error(str(error))
    elif st.button("Start IPFIX for this source", type="primary"):
        try:
            client.start_ipfix(
                {
                    "collector_id": selected_collector_id,
                    "selected_clusters": selected_clusters,
                    "manual_exporters": manual_exporters.splitlines(),
                    "excluded_exporters": excluded_exporters,
                }
            )
            st.success("IPFIX listener started on UDP 4739.")
            st.rerun()
        except AgentDashboardClientError as error:
            st.error(str(error))

else:
    st.subheader("Collector status")
    render_agent_status()
