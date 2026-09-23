# VMware Migration Deployable Product

This folder is an isolated foundation for the next architecture. It does not modify
the existing standalone VMware discovery application.

## Components

- `collector_agent`: customer-side registration and health client. Future collection
  modules will run here, close to vCenter, NSX/vDS, and IPFIX exporters.
- `control_plane`: OCI-hosted FastAPI service for collector registration, health,
  idempotent observation ingestion, and later the Streamlit dashboard.

The control plane never accepts vCenter or guest operating-system credentials.
Those credentials belong only to the customer-side collector runtime.

## Phase 1 capabilities

1. A control-plane administrator creates a short-lived, one-use enrollment code.
2. A customer collector registers with that code and receives a unique collector ID
   plus an agent token, returned once.
3. The agent authenticates heartbeats and normalized connection observations.
4. The API uses collector-scoped sequence numbers to safely deduplicate retries.

The current bootstrap uses a per-agent bearer token over HTTPS. Production deployment
must terminate TLS and move to per-agent mTLS certificates, certificate rotation, and
revocation before customer release.

## Collector-agent runtime foundation

The agent now exposes a loopback-only local API for appliance setup. It can register
with the control plane, retain vCenter and guest credentials only in memory, send a
periodic heartbeat, synchronize vCenter VM inventory, and forward normalized IPFIX
observations. Guest Operations collection is a later increment.

Run it for development only:

```powershell
& ..\.venv\Scripts\python.exe -m uvicorn collector_agent.main:create_app --factory --host 127.0.0.1 --port 8443
```

Do not expose this local setup API remotely until local TLS and administrator
authentication are implemented.

## Local development only

```powershell
Copy-Item .env.example .env
# Set CONTROL_PLANE_ADMIN_API_KEY to a unique value.
& ..\.venv\Scripts\python.exe -m uvicorn control_plane.main:create_app --factory --reload
```

The default SQLite database is development-only. Use a managed OCI database and TLS
termination for a real control-plane deployment.

Start the familiar Streamlit-style control-plane dashboard in a separate terminal:

```powershell
& ..\.venv\Scripts\python.exe -m streamlit run control_plane/dashboard.py
```

The dashboard reads `CONTROL_PLANE_DASHBOARD_API_URL` and
`CONTROL_PLANE_DASHBOARD_API_KEY` from the deployment environment. These are not user
inputs. In development it falls back to the administrator key; production must supply a
separate read-only dashboard key. It shows registered collectors, each collector's
active uploaded VM inventory, and collector-scoped IPFIX connection reporting; it never
receives vCenter credentials.

The **Migration Waves** page selects one collector and an observed time range (1 day,
5 days, 1 week, 1 month, or a custom date range). It builds a wave from internal TCP
connections between active, powered-on inventory VMs. Powered-off VMs are excluded from
wave assignment. Connections to destinations outside the eligible inventory appear in
the selected wave diagram as external dependencies but do not merge migration waves.
The diagram is capped at 100 unique edges to keep it usable.

The **Enroll Collector** page is an administrator-only operation. It creates a one-use,
short-lived enrollment code using `CONTROL_PLANE_ADMIN_API_KEY`, shows it once, and does
not store the plaintext code in the database. Protect this dashboard with an OCI
administrator-only identity-aware proxy before exposing it to users; the current local
Streamlit process does not implement end-user authentication.

The current dashboard key is an operator-level credential, not end-user tenant access
control. Before a multi-customer release, place the dashboard behind OCI identity-aware
authentication and enforce tenant-scoped authorization in every dashboard query (for
example, with a tenant claim plus database row-level security). Do not expose this
development dashboard directly to customers.

The same page can create a short-lived, one-use **reconnection code** for an existing
collector. After an appliance restart, choose **Reconnect existing collector** in the
local collector dashboard, enter the existing collector ID and reconnection code, and
the control plane rotates its agent token while retaining the same collector history.

Start the customer-side local collector dashboard only on the appliance loopback interface:

```powershell
& ..\.venv\Scripts\python.exe -m streamlit run collector_agent/dashboard.py --server.address 127.0.0.1
```

It provides registration, local vCenter setup, inventory synchronization, IPFIX setup,
and safe status. Agent Status and IPFIX counters refresh automatically every 15 seconds;
each page also has a **Refresh now** option. It does not expose any customer credentials
to the control plane.

The control plane calculates collector health from authenticated heartbeats. A collector
is displayed as **Offline** when no successful heartbeat is received for 180 seconds.

## IPFIX collector setup

1. Register the collector and synchronize inventory from the collector dashboard.
2. Open **IPFIX Setup**. The agent proposes ESXi `vmk0` IP addresses grouped by cluster.
   Select the clusters to trust. Every ESXi candidate in those clusters is included by
   default, but individual hosts can be excluded before starting the listener. Enter a
   specific exporter IP when the IPFIX source is different from the `vmk0` address.
3. Start the listener. It binds to all collector appliance interfaces on UDP `4739` and
   accepts datagrams only from the selected or explicitly entered individual IP addresses.
4. In NSX/vSphere, configure the IPFIX collector destination as the appliance's reachable
   IP address and UDP port `4739`.
5. Confirm the dashboard counters increase. TCP and UDP flow metadata from an active,
   discovered inventory VM is normalized and sent to the control plane; packet payloads
   and flows from unknown source VMs are discarded.

UDP records are reporting-only. The control plane permanently excludes all UDP records
from migration-wave assignment. UDP destination ports 53 (DNS), 123 (NTP), 67-68 (DHCP),
and 546-547 (DHCPv6) are labelled as background infrastructure in the connection report.
This prevents shared infrastructure services from incorrectly combining unrelated VMs
into one migration wave.

`vmk0` is a useful default, not a universal exporter-source guarantee. Keep the explicit
exporter-IP option for environments that export from another ESXi/NSX address.

## Customer network requirements

- Collector to vCenter: TCP 443.
- NSX/vDS exporters to collector, if IPFIX is enabled: UDP 4739, restricted to known
  exporter addresses.
- Collector to OCI control plane: outbound TCP 443 only.

No OCI component needs an inbound connection to the customer environment.

## Security boundary

- Do not place `VCENTER_PASSWORD`, guest passwords, or enrollment codes in control-
  plane logs, telemetry, or databases.
- Enrollment codes are stored only as SHA-256 digests and become unusable after one
  registration or expiry.
- Agent tokens are stored only as SHA-256 digests by the control plane.
- This development version intentionally keeps the agent token and all customer
  credentials only in memory. After an agent restart, the appliance must be registered
  and configured again. Persisting an agent identity requires an approved protected
  local secret store, rotation, and revocation design before customer release.
