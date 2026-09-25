"""Local Streamlit setup and health dashboard for a collector appliance."""

from __future__ import annotations

import html

import streamlit as st

try:  # Supports `streamlit run collector_agent/dashboard.py`.
    from collector_agent.dashboard_client import AgentDashboardClient, AgentDashboardClientError
except ModuleNotFoundError:  # pragma: no cover - Streamlit executes this as a script.
    from dashboard_client import AgentDashboardClient, AgentDashboardClientError

st.set_page_config(page_title="Migration Discovery Collector", page_icon="M", layout="wide")
st.markdown(
    """
    <style>
    .agent-card { min-height: 102px; padding: 1rem; border: 1px solid rgba(255,255,255,.12);
      border-radius: 16px; background: linear-gradient(145deg, #263244, #141b27);
      box-shadow: 0 10px 24px rgba(0,0,0,.20), inset 0 1px 0 rgba(255,255,255,.08); }
    .agent-card__label { color: #B7C5D6; font-size: .78rem; font-weight: 600; }
    .agent-card__value { margin-top: .45rem; color: #F7FAFC; font-size: 1.35rem; font-weight: 700; }
    .agent-status-table-space { height: 1.25rem; }
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


def source_options(status: dict[str, object], source_type: str | None = None) -> dict[str, str]:
    sources = status.get("sources", [])
    if not isinstance(sources, list):
        return {}
    return {
        f"{item['display_name']} — {item['collector_id']}": item["collector_id"]
        for item in sources
        if isinstance(item, dict)
        and (source_type is None or item.get("source_type") == source_type)
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
    registered_sources, ipfix_exporters = st.columns(2)
    with registered_sources:
        card("Registered sources", str(len(sources)))
    with ipfix_exporters:
        card("IPFIX exporters", str(sum(item["ipfix_exporter_count"] for item in sources)))
    st.markdown("<div class='agent-status-table-space'></div>", unsafe_allow_html=True)
    if sources:
        st.dataframe(
            sources,
            column_config={
                "display_name": "Source",
                "source_type": "Source type",
                "collector_id": "Collector ID",
                "vcenter_configured": "vCenter configured",
                "azure_configured": "Azure configured",
                "aws_configured": "AWS configured",
                "aws_flow_logs_configured": "AWS Flow Logs configured",
                "aws_flow_log_status": "AWS Flow Log status",
                "aws_flow_log_detail": "AWS Flow Log detail",
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
        ("Collector Agent Setup", "VMware Setup", "Azure Setup", "AWS Setup", "Agent Status"),
        label_visibility="collapsed",
    )

st.title("Migration Discovery Collector")
st.caption("Register collection sources, configure supported platforms, and monitor local health.")

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
        source_type = st.selectbox("Source type", ("VMware", "Azure", "AWS"))
        display_name = st.text_input(
            "Source name",
            value={"VMware": "vmware-vcenter-01", "Azure": "azure-01", "AWS": "aws-01"}[
                source_type
            ],
        )
        register = st.form_submit_button("Register source")
    if register:
        try:
            result = client.register(
                {
                    "control_plane_url": control_plane_url,
                    "tenant_id": tenant_id,
                    "enrollment_code": enrollment_code,
                    "display_name": display_name,
                    "source_type": source_type.lower(),
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
            reconnect_source_type = st.selectbox("Source type", ("VMware", "Azure", "AWS"))
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
                        "source_type": reconnect_source_type.lower(),
                    }
                )
                st.success(f"Source reconnected: {result['collector_id']}.")
            except AgentDashboardClientError as error:
                st.error(str(error))
    if current_status["sources"]:
        table_rows = [{**source, "unregister": False} for source in current_status["sources"]]
        table_columns = tuple(table_rows[0])
        table_revision = st.session_state.get("registered_sources_table_revision", 0)
        edited_sources = st.data_editor(
            table_rows,
            column_config={
                "unregister": st.column_config.CheckboxColumn(
                    "Unregister",
                    help="Select one source, then confirm the local unregister action below.",
                    default=False,
                )
            },
            column_order=table_columns,
            disabled=[column for column in table_columns if column != "unregister"],
            hide_index=True,
            key=f"registered_sources_table_{table_revision}",
            use_container_width=True,
        )
        selected_collector_ids = [
            str(row["collector_id"])
            for row in edited_sources
            if isinstance(row, dict)
            and isinstance(row.get("collector_id"), str)
            and row.get("unregister") is True
        ]
        if len(selected_collector_ids) > 1:
            st.warning("Select only one source to unregister.")
        confirm_unregister = st.checkbox(
            "I understand this stops local collection and removes local credentials for the "
            "selected source. Control-plane history is retained.",
            key="confirm_unregister_source",
        )
        if st.button(
            "Unregister selected source from this appliance",
            type="secondary",
            disabled=len(selected_collector_ids) != 1 or not confirm_unregister,
        ):
            try:
                result = client.unregister(selected_collector_ids[0])
                st.session_state["registered_sources_table_revision"] = table_revision + 1
                st.success(
                    f"Source {result['collector_id']} was unregistered from this appliance. "
                    "Its control-plane history was retained."
                )
                st.rerun()
            except AgentDashboardClientError as error:
                st.error(str(error))
    else:
        st.info("No registered source is available to unregister.")

elif page == "VMware Setup":
    st.subheader("VMware setup")
    options = source_options(current_status, "vmware")
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

elif page == "Azure Setup":
    st.subheader("Azure setup")
    options = source_options(current_status, "azure")
    if not options:
        st.info("Register or reconnect an Azure source before configuring Azure collection.")
        st.stop()
    selected_label = st.selectbox("Azure source", list(options))
    selected_collector_id = options[selected_label]
    selected_source = next(
        item for item in current_status["sources"] if item["collector_id"] == selected_collector_id
    )
    st.caption(
        "Azure application credentials are retained only in memory for the selected source. "
        "They are never uploaded to the OCI control plane."
    )
    st.info(
        "Selected source inventory in local agent: "
        f"{selected_source['last_inventory_vm_count']} VM(s). "
        f"Collector ID: {selected_collector_id}"
    )
    with st.form("azure_credentials"):
        azure_tenant_id = st.text_input("Microsoft Entra tenant ID")
        azure_client_id = st.text_input("Application (client) ID")
        azure_client_secret = st.text_input("Client secret value", type="password")
        subscription_ids = st.text_area("Azure subscription IDs (one per line)")
        configure_azure = st.form_submit_button("Store credentials in memory")
    if configure_azure:
        try:
            client.configure_azure_credentials(
                {
                    "collector_id": selected_collector_id,
                    "azure_tenant_id": azure_tenant_id,
                    "azure_client_id": azure_client_id,
                    "azure_client_secret": azure_client_secret,
                    "subscription_ids": subscription_ids.splitlines(),
                }
            )
            st.success("Azure credentials are held in collector memory for this source.")
        except AgentDashboardClientError as error:
            st.error(str(error))

    st.subheader("Synchronize inventory")
    st.caption(
        "The initial Azure connector inventories virtual machines and their private NIC IPs. "
        "Azure flow-log ingestion will be added separately."
    )
    if st.button("Sync Azure inventory", type="primary"):
        try:
            result = client.sync_inventory(selected_collector_id)
            st.success(
                f"Inventory synchronized for Collector ID {selected_collector_id}: "
                f"{result['discovered']} discovered, {result['accepted']} accepted "
                "by the control plane."
            )
        except AgentDashboardClientError as error:
            st.error(str(error))

elif page == "AWS Setup":
    st.subheader("AWS setup")
    options = source_options(current_status, "aws")
    if not options:
        st.info("Register or reconnect an AWS source before configuring AWS collection.")
        st.stop()
    selected_label = st.selectbox("AWS source", list(options))
    selected_collector_id = options[selected_label]
    selected_source = next(
        item for item in current_status["sources"] if item["collector_id"] == selected_collector_id
    )
    st.caption(
        "AWS credentials are retained only in memory for the selected source. "
        "They are never uploaded to the OCI control plane."
    )
    st.info(
        "Selected source inventory in local agent: "
        f"{selected_source['last_inventory_vm_count']} VM(s). "
        f"Collector ID: {selected_collector_id}"
    )
    with st.form("aws_credentials"):
        aws_region = st.text_input("AWS Region", placeholder="ap-southeast-2")
        aws_access_key_id = st.text_input("AWS access key ID (optional)")
        aws_secret_access_key = st.text_input("AWS secret access key (optional)", type="password")
        aws_session_token = st.text_input("AWS session token (optional)", type="password")
        st.caption(
            "Leave credential fields empty only when the appliance has an AWS SDK credential "
            "provider configured, such as IAM Roles Anywhere."
        )
        configure_aws = st.form_submit_button("Store credentials in memory")
    if configure_aws:
        try:
            client.configure_aws_credentials(
                {
                    "collector_id": selected_collector_id,
                    "aws_region": aws_region,
                    "aws_access_key_id": aws_access_key_id or None,
                    "aws_secret_access_key": aws_secret_access_key or None,
                    "aws_session_token": aws_session_token or None,
                }
            )
            st.success(
                "AWS authentication configuration is held in collector memory for this source."
            )
        except AgentDashboardClientError as error:
            st.error(str(error))

    st.subheader("Synchronize inventory")
    st.caption(
        "The initial AWS connector inventories EC2 instances and their private interface IPs "
        "in the selected Region. VPC Flow Log ingestion will be added separately."
    )
    if st.button("Sync AWS inventory", type="primary"):
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
    st.subheader("AWS VPC Flow Logs")
    st.caption(
        "The collector reads only new gzip/text VPC Flow Log objects from this customer-owned "
        "S3 prefix. It polls automatically with the agent heartbeat and can also be refreshed now."
    )
    with st.form("aws_flow_logs"):
        s3_bucket_name = st.text_input("S3 bucket name")
        s3_prefix = st.text_input("S3 prefix", value="oci-migration/")
        configure_aws_flow_logs = st.form_submit_button("Store VPC Flow Log location in memory")
    if configure_aws_flow_logs:
        try:
            client.configure_aws_flow_logs(
                {
                    "collector_id": selected_collector_id,
                    "s3_bucket_name": s3_bucket_name,
                    "s3_prefix": s3_prefix,
                }
            )
            st.success("AWS VPC Flow Log location is held in collector memory for this source.")
        except AgentDashboardClientError as error:
            st.error(str(error))
    if selected_source.get("aws_flow_logs_configured"):
        flow_log_columns = st.columns(3)
        flow_log_columns[0].metric(
            "Objects processed", selected_source.get("aws_flow_log_objects_processed", 0)
        )
        flow_log_columns[1].metric(
            "Observations uploaded", selected_source.get("aws_flow_log_observations_uploaded", 0)
        )
        flow_log_columns[2].metric(
            "Flow Log status", selected_source.get("aws_flow_log_status", "Waiting")
        )
        if selected_source.get("aws_flow_log_last_sync_at"):
            st.caption(f"Last VPC Flow Log sync: {selected_source['aws_flow_log_last_sync_at']}")
        if st.button("Sync VPC Flow Logs now"):
            try:
                result = client.sync_aws_flow_logs(selected_collector_id)
                st.success(
                    "VPC Flow Logs synchronized: "
                    f"{result['objects_processed']} object(s) processed, "
                    f"{result['observations_uploaded']} observation(s) uploaded."
                )
                st.rerun()
            except AgentDashboardClientError as error:
                st.error(str(error))

else:
    st.subheader("Collector status")
    render_agent_status()
