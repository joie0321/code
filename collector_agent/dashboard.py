"""Local Streamlit setup and health dashboard for a collector appliance."""

from __future__ import annotations

import html

import streamlit as st

try:  # Supports `streamlit run collector_agent/dashboard.py`.
    from collector_agent.dashboard_client import AgentDashboardClient, AgentDashboardClientError
except ModuleNotFoundError:  # pragma: no cover - Streamlit executes this as a script.
    from dashboard_client import AgentDashboardClient, AgentDashboardClientError

st.set_page_config(page_title="VMware Migration Collector", page_icon="🧭", layout="wide")
st.markdown(
    """
    <style>
    .agent-card { min-height: 112px; padding: 1rem; border: 1px solid rgba(255,255,255,.12);
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


@st.fragment(run_every="15s")
def render_agent_status() -> None:
    """Refresh lightweight local agent health without contacting vCenter."""

    try:
        current_status = client.status()
    except AgentDashboardClientError as error:
        st.warning(str(error))
        return
    st.caption("Status refreshes automatically every 15 seconds.")
    if st.button("Refresh now", key="refresh_agent_status"):
        st.rerun()
    left, middle, right = st.columns(3)
    with left:
        card("Registration", str(current_status["registration"]).replace("_", " ").title())
    with middle:
        card(
            "vCenter configuration",
            "Ready" if current_status["vcenter_configured"] else "Required",
        )
    with right:
        card("Last inventory VM count", str(current_status["last_inventory_vm_count"]))
    st.divider()
    st.write(
        "Last heartbeat:", current_status["last_heartbeat_at"] or "Waiting for first heartbeat"
    )
    st.write("Last inventory sync:", current_status["last_inventory_sync_at"] or "Not synchronized")
    if current_status["last_error"]:
        st.warning(current_status["last_error"])
    else:
        st.success("No current collector errors.")


@st.fragment(run_every="15s")
def render_ipfix_status() -> None:
    """Refresh local IPFIX counters without changing listener configuration."""

    try:
        current_setup = client.ipfix_setup()
    except AgentDashboardClientError as error:
        st.warning(str(error))
        return
    listener = current_setup["listener"]
    st.caption("IPFIX counters refresh automatically every 15 seconds.")
    if st.button("Refresh now", key="refresh_ipfix_status"):
        st.rerun()
    if listener["status"] == "running":
        st.success("IPFIX listener is running.")
    else:
        st.info("IPFIX listener is stopped.")
    status_columns = st.columns(4)
    status_columns[0].metric("Datagrams accepted", listener["datagrams_received"])
    status_columns[1].metric("Datagrams rejected", listener["datagrams_rejected"])
    status_columns[2].metric("TCP flows found", listener["flows_received"])
    status_columns[3].metric("Observations uploaded", listener["observations_stored"])
    if listener["last_error"]:
        st.warning(listener["last_error"])


with st.sidebar:
    st.header("Collector Appliance")
    st.caption("Local setup only. Customer credentials remain on this appliance.")
    page = st.radio(
        "Navigation", ("Setup", "Agent Status", "IPFIX Setup"), label_visibility="collapsed"
    )

st.title("VMware Migration Collector")
st.caption(
    "Register this appliance, synchronize vCenter inventory, and monitor local collection health."
)

try:
    client.status()
except AgentDashboardClientError as error:
    st.error(str(error))
    st.info("Start the local collector-agent API before opening this dashboard.")
    st.stop()

if page == "Setup":
    st.subheader("1. Register or reconnect this collector")
    st.write("Create the one-time enrollment code from the OCI control-plane dashboard.")
    with st.form("registration"):
        control_plane_url = st.text_input("OCI control-plane URL", value="http://127.0.0.1:8100")
        tenant_id = st.text_input("Customer tenant ID", value="lab-customer")
        enrollment_code = st.text_input("One-time enrollment code", type="password")
        display_name = st.text_input("Collector name", value="collector-appliance-01")
        register = st.form_submit_button("Register collector")
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
            st.success(f"Collector registered: {result['status']}.")
        except AgentDashboardClientError as error:
            st.error(str(error))

    with st.expander("Reconnect an existing collector after an appliance restart"):
        st.caption(
            "Generate a one-time reconnection code for this collector ID from the control-plane "
            "administrator dashboard. This rotates the agent token while preserving history."
        )
        with st.form("reconnection"):
            reconnect_control_plane_url = st.text_input(
                "OCI control-plane URL", value="http://127.0.0.1:8100", key="reconnect_url"
            )
            collector_id = st.text_input("Existing collector ID")
            reconnection_code = st.text_input("One-time reconnection code", type="password")
            reconnect = st.form_submit_button("Reconnect existing collector")
        if reconnect:
            try:
                result = client.reconnect(
                    {
                        "control_plane_url": reconnect_control_plane_url,
                        "collector_id": collector_id,
                        "reconnection_code": reconnection_code,
                    }
                )
                st.success(f"Collector reconnected: {result['collector_id']}.")
            except AgentDashboardClientError as error:
                st.error(str(error))

    st.divider()
    st.subheader("2. Configure vCenter locally")
    st.caption(
        "These credentials are sent only to the loopback collector service and retained in memory."
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
                    "vcenter_host": host,
                    "vcenter_username": username,
                    "vcenter_password": password,
                    "verify_tls": verify_tls,
                }
            )
            st.success("vCenter credentials are held in collector memory.")
        except AgentDashboardClientError as error:
            st.error(str(error))

    st.subheader("3. Synchronize inventory")
    if st.button("Sync vCenter inventory", type="primary"):
        try:
            result = client.sync_inventory()
            st.success(
                f"Inventory synchronized: {result['discovered']} VMs uploaded to the control plane."
            )
        except AgentDashboardClientError as error:
            st.error(str(error))

elif page == "Agent Status":
    st.subheader("Collector status")
    render_agent_status()

else:
    st.subheader("IPFIX setup")
    st.caption(
        "The collector listens on all appliance interfaces at UDP 4739. Select the ESXi "
        "vmk0 candidates to trust, then configure NSX/vSphere to export to this appliance IP."
    )
    try:
        ipfix_setup = client.ipfix_setup()
    except AgentDashboardClientError as error:
        st.error(str(error))
        st.stop()

    candidates = ipfix_setup["exporter_candidates"]
    listener = ipfix_setup["listener"]
    render_ipfix_status()

    if candidates:
        clusters = sorted({item["cluster"] or "Unclustered" for item in candidates})
        selected_clusters = st.multiselect(
            "Clusters to trust",
            clusters,
            default=clusters,
            help="The default includes every discovered cluster with a vmk0 candidate.",
        )
        selected_candidate_hosts = [
            item for item in candidates if (item["cluster"] or "Unclustered") in selected_clusters
        ]
        st.caption(
            "Each ESXi exporter is included by default. Clear an individual host to exclude all "
            "of its displayed vmk0 candidate addresses from the listener allowlist."
        )
        excluded_exporters: list[str] = []
        for candidate_index, item in enumerate(selected_candidate_hosts):
            cluster_label = item["cluster"] or "Unclustered"
            included = st.checkbox(
                f"Include {item['host_name']} ({cluster_label}) — {', '.join(item['vmk0_ips'])}",
                value=True,
                key=f"ipfix_exporter_include_{candidate_index}_{item['host_name']}",
            )
            if not included:
                excluded_exporters.extend(item["vmk0_ips"])
        st.dataframe(
            [
                {
                    "Cluster": item["cluster"] or "Unclustered",
                    "ESXi host": item["host_name"],
                    "vmk0 candidate address": ", ".join(item["vmk0_ips"]),
                }
                for item in candidates
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        selected_clusters = []
        excluded_exporters = []
        st.warning(
            "No vmk0 candidates are available. Synchronize inventory with an account that can read "
            "ESXi host networking, or enter the exporter IPs explicitly below."
        )

    manual_exporters = st.text_area(
        "Additional exporter IPs (optional)",
        help="One individual IPv4 or IPv6 address per line. Do not enter networks or ranges.",
    )
    if listener["status"] == "running":
        if st.button("Stop IPFIX listener"):
            try:
                client.stop_ipfix()
                st.rerun()
            except AgentDashboardClientError as error:
                st.error(str(error))
    elif st.button("Start IPFIX listener", type="primary"):
        try:
            client.start_ipfix(
                {
                    "selected_clusters": selected_clusters,
                    "manual_exporters": manual_exporters.splitlines(),
                    "excluded_exporters": excluded_exporters,
                }
            )
            st.success("IPFIX listener started on UDP 4739.")
            st.rerun()
        except AgentDashboardClientError as error:
            st.error(str(error))
