# Migration Discovery

Migration Discovery helps teams understand VM inventory, observed network traffic, workload dependencies, and migration-wave options before planning a migration. It supports VMware, AWS, and Azure sources through customer-side Collector Agents and a central Control Plane.

The Collector normalizes inventory and accepted flow metadata before upload. The Control Plane provides current and historical estate views, automatic wave recommendations, dependency review, readiness, versioned migration plans, plan-drift detection, and PDF/Excel exports.

## Current product status

| Capability | VMware | AWS | Azure |
| --- | --- | --- | --- |
| VM inventory | vCenter | EC2, one Region per source | Resource Graph, one or more subscriptions |
| Network evidence | Live IPFIX | VPC Flow Logs in S3 | Virtual Network Flow Logs in Blob Storage |
| Automatic wave recommendation | Yes | Yes | Yes |
| Dependency review/readiness | Yes | Yes | Yes |
| Versioned plans, drift, PDF/Excel reports | Yes | Yes | Yes |

Not implemented in the current release: guest OS application profiling, automated migration execution, packet-payload collection, CIDR/non-cloud source discovery, persistent local Collector credential storage, and AI-agent functionality. Guest credentials can be entered for VMware setup but are not used to profile applications.

## Architecture and data boundary

```text
Customer environment                                      Control Plane
--------------------                                      -------------------------
VMware / AWS / Azure APIs --+
Flow exporters / log store ---> Collector Agent -- HTTPS --> API + PostgreSQL + Dashboard
Source credentials         --+
```

The Collector stays in the customer environment. It keeps source credentials, raw log locations, and raw flow data local. The Control Plane receives normalized records such as VM ID/name/IP/power state and connection source/destination IP, port, protocol, and timestamp.

The Control Plane does **not** receive vCenter/AWS/Azure credentials, guest credentials, raw IPFIX datagrams, raw cloud flow-log files, or packet payloads.

## How automatic waves work

1. The Collector uploads a complete inventory snapshot and accepted connection observations.
2. For the selected reporting range, the Control Plane selects VMs that are active and `poweredOn`.
3. It creates a relationship only for observed **TCP** traffic whose source and destination map to eligible VMs in the same Collector source.
4. Connected components of this internal TCP graph become recommended waves.

UDP is visible in reporting but never merges waves. This deliberately prevents DNS, NTP, DHCP, and similar shared infrastructure services from grouping unrelated workloads. External endpoints and unavailable internal VMs also never merge automatic waves; they remain review evidence. A powered-on VM without internal TCP traffic is a singleton recommendation.

An automatic wave is evidence for planning, not cutover approval.

## Current and historical data

### Current Environment

The **Current Environment** page is an estate/evidence page, independent of an approved plan.

- **Latest** shows traffic from the latest successful inventory sync to now.
- **Historical period** supports 1 day, 5 days, 1 week, 1 month, or a custom range.
- Historical inventory uses retained inventory-state evidence at the selected date. A VM discovered after the selected period is not shown in that historical period.
- A VM that existed during the period but is now powered off or removed remains visible in historical evidence. In the graph, hover to see its current status; powered-off nodes are orange with a warning icon and removed nodes are red with a warning icon.
- Current observed connections are deduplicated by logical source/destination/protocol/port and identify `Observed once` or `Observed repeatedly`.

### Inventory lifecycle

- Empty inventory snapshots are rejected when trusted inventory already exists.
- A VM absent from a later snapshot becomes `missing` and is excluded from current waves.
- After 24 hours continuously absent, it becomes inactive/removed from current inventory.
- Historical state and connection evidence are retained. Removing a VM from AWS, Azure, or vCenter does not erase prior evidence.

## Source-side preparation

Each vCenter, AWS Region, or Azure source is registered as its own Collector identity. One Collector appliance can operate several sources. A VMware IPFIX exporter IP can belong to only one source on that appliance.

### Common requirements

- Collector host running Python 3.11+.
- Outbound HTTPS from Collector to Control Plane.
- Network reachability from Collector to relevant platform APIs and log storage.
- DNS/proxy/firewall/TLS trust configured for the target endpoints.
- A separate Control Plane enrollment code for every newly registered source.
- No inbound connection from the Control Plane to the customer environment is required.

| Activity | Default frequency |
| --- | --- |
| Heartbeat | 60 seconds |
| Inventory synchronization | 24 hours; configurable from 5 minutes to 7 days via `INVENTORY_SYNC_INTERVAL_SECONDS` |
| AWS VPC Flow Log polling | 5 minutes |
| Azure Virtual Network Flow Log polling | 5 minutes |
| VMware IPFIX | Continuous listener/upload when flows arrive |

## VMware requirements

### vCenter account

Create a dedicated vCenter service account with read access at the vCenter root or every in-scope datacenter/folder/cluster, with propagation enabled. The built-in **Read-only** role is the usual starting point. It must be able to read:

- VM UUID, name, power state, operating-system information, cluster, and folder;
- guest/VM network information, particularly IP addresses; and
- ESXi host/cluster information used to propose exporter candidates.

The Collector connects to vCenter over TCP 443. Keep TLS certificate verification enabled and install/trust the issuing vCenter CA on the Collector host. If vCenter does not return a VM's IP address, the Collector cannot map that VM's IPFIX source traffic.

### IPFIX export

Configure IPFIX export from vDS, ESXi, NSX, or the chosen VMware telemetry point to the Collector's reachable IP address on UDP 4739. Prepare:

- trusted ESXi/NSX/vDS exporter IP addresses;
- firewall access from exporters to the Collector on UDP 4739;
- the clusters/exporters that should be trusted; and
- confirmation that no exporter IP is assigned to another source on this Collector appliance.

The Collector accepts IPFIX datagrams only from selected/manual individual exporter IPs. It uploads a flow only if its source IP maps to a current powered-on discovered VM. Unknown source flows are discarded.

### VMware checklist

1. Confirm Collector-to-vCenter TCP 443 connectivity and CA trust.
2. Create/test the read-only vCenter account.
3. Confirm in-scope VMs return guest IPs and power state.
4. Configure exporter destination as `<collector-address>:4739/UDP`.
5. Permit exporter-to-Collector UDP 4739.
6. Register the VMware source, configure vCenter locally, and synchronize inventory.
7. Select exporters in **IPFIX Setup**, start the listener, generate test TCP traffic, and confirm observations arrive.

## AWS requirements

An AWS source represents one AWS Region. Register a separate source for each Region.

### Credential method

Use either an IAM user/temporary access-key ID plus secret access key (optional session token), or an AWS SDK default credential provider available to the Collector, such as an approved IAM Roles Anywhere credential process. Credentials remain in the local Collector process and are never sent to the Control Plane.

### EC2 inventory IAM policy

The implemented inventory connector requires `ec2:DescribeInstances`:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "ReadEc2Inventory",
    "Effect": "Allow",
    "Action": ["ec2:DescribeInstances"],
    "Resource": "*"
  }]
}
```

The Collector reads instance ID/state, private interface IPs, VPC ID, Region, and the `Name` tag where present. It does not create, stop, modify, or delete instances.

### VPC Flow Logs to S3

Enable VPC Flow Logs for the in-scope network and deliver them to a customer-owned S3 bucket/prefix. The connector expects the following custom field order:

```text
version account-id vpc-id subnet-id instance-id interface-id srcaddr dstaddr
srcport dstport protocol packets bytes start end action tcp-flags log-status
```

Usable evidence must have `action=ACCEPT` and `log-status=OK`. `NODATA` does not represent connection evidence. The app accepts TCP protocol `6` and UDP protocol `17`, using the flow-log end time as the observation timestamp.

### S3 IAM policy

Grant read access only to the configured bucket and prefix. Replace placeholders:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListConfiguredFlowLogPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<bucket-name>",
      "Condition": {"StringLike": {"s3:prefix": ["<flow-log-prefix>/*"]}}
    },
    {
      "Sid": "ReadConfiguredFlowLogObjects",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<bucket-name>/<flow-log-prefix>/*"
    }
  ]
}
```

The current **AWS Setup** UI asks for the S3 **bucket name**, not an ARN. Enter only `<bucket-name>` there and configure the flow-log object path separately as the prefix. The ARN is used in the IAM policy above.

The Collector reads only unseen `.log.gz` objects under that prefix, up to 250 objects each poll. It does not write or delete S3 objects. It rejects objects larger than 16 MiB compressed or 64 MiB uncompressed.

### AWS checklist

1. Choose the Region and credential method.
2. Grant `ec2:DescribeInstances`.
3. Enable VPC Flow Logs and verify accepted records reach the chosen S3 prefix.
4. Grant prefix-limited `s3:ListBucket` and `s3:GetObject`.
5. Register/configure the AWS source and synchronize inventory.
6. Confirm running instances show private IPs.
7. Configure bucket/prefix in **AWS Setup**, manually sync, and verify accepted source-matched flows appear.

## Azure requirements

### Entra application

Create a Microsoft Entra application registration and client secret. Record:

- Directory (tenant) ID;
- Application (client) ID; and
- client **secret value**—not secret ID.

The client secret value is shown only once in Azure. Store it in an approved customer secret-management process before entering it in local Collector configuration.

### Subscription/Resource Graph access

Select the subscriptions to discover and assign the application the built-in **Reader** role at every selected subscription, or an equivalent custom role allowing:

- `Microsoft.Compute/virtualMachines/read`;
- `Microsoft.Network/networkInterfaces/read`; and
- Resource Graph queries in the selected subscriptions.

Register the `Microsoft.ResourceGraph` resource provider for each selected subscription. The Collector queries Resource Graph for VMs and NIC private IP configurations; unavailable NIC/Resource Graph access produces incomplete flow matching.

### Virtual Network Flow Logs and Blob access

Enable Azure Virtual Network Flow Logs for in-scope virtual networks and deliver logs to a customer-owned Storage account. Configure the Collector with:

- Storage account name;
- container name, normally `insights-logs-flowlogflowevent`; and
- blob prefix, normally beginning `flowLogResourceID=/`.

Grant the Entra application:

- **Storage Blob Data Reader** on the configured flow-log container; and
- **Reader** on the Storage account.

The connector reads unseen `.json` blobs below the prefix, up to 250 blobs per poll. It supports Azure VNet Flow Log v4 tuples, accepts TCP `6`/UDP `17`, discards denied tuples, and uploads only source-IP-matched active inventory traffic. Each blob must be 64 MiB or less.

### Azure checklist

1. Create Entra app/client secret; record tenant ID, client ID, and secret value.
2. Assign Reader to selected subscriptions and register `Microsoft.ResourceGraph`.
3. Enable Virtual Network Flow Logs and confirm logs arrive in the Storage account.
4. Grant Storage Blob Data Reader on the flow-log container and Reader on the Storage account.
5. Register/configure the Azure source and synchronize inventory.
6. Confirm VM names, state, and private NIC IPs appear.
7. Configure storage account/container/prefix, manually sync, and confirm accepted traffic appears.

## Dependency review, readiness, and plans

Dependency Review presents external endpoints, shared infrastructure, shared-service candidates, unavailable internal VMs, and internal dependencies. Planners can save one common decision/note across multiple selected rows.

Readiness uses the selected observation window:

- **Confidence** considers observation coverage, inventory age, collector check-in, and whether observations exist.
- **Risk** is High for high-impact dependencies, Medium for action-required/investigate items, and Low when none remain.
- **Ready for approval** means High confidence and Low risk. It is not automatic cutover approval.

Migration plans are Collector-scoped and versioned. A draft can be created from the automatic recommendation or cloned from an approved plan. The planner can assign/move/exclude individual or multiple VMs. Approval freezes assignments and a dependency baseline, makes the version read-only, and supersedes the prior approved plan for that Collector.

Plan drift is continuously calculated against the latest approved plan; it identifies approved VMs now missing/inactive, new eligible VMs not represented in the plan, and new post-approval review dependencies. It informs planners but does not change the approved plan automatically.

## Install and run

### Development/local

```powershell
python -m pip install -e ".[dev]"

# Control Plane API
python -m uvicorn control_plane.main:create_app --factory --host 127.0.0.1 --port 8100

# Control Plane dashboard
python -m streamlit run control_plane/dashboard.py

# Optional Collector setup API
python -m uvicorn collector_agent.main:create_app --factory --host 127.0.0.1 --port 8443

# Optional Collector dashboard
python -m streamlit run collector_agent/dashboard.py --server.address 127.0.0.1
```

Copy `.env.example` to `.env` and set a unique `CONTROL_PLANE_ADMIN_API_KEY` of at least 32 characters. SQLite is development-only.

### Production

Use either OCI Vault-backed PostgreSQL (`DATABASE_MODE=oci_vault`) or customer-managed external PostgreSQL (`DATABASE_MODE=external`). Use a least-privilege database role, TLS verification (`sslmode=verify-full`), protected database-secret retrieval, and HTTPS for API/dashboard access. Alembic migrations run at API startup; back up the database before upgrades.

## Operations and security limitations

- The Control Plane marks a Collector offline after 180 seconds without a successful authenticated heartbeat.
- Unregistering a source from the Collector removes only local in-memory configuration; Control Plane history remains.
- Deleting a Collector from the Control Plane permanently deletes that Collector's inventory, observations, batches, review decisions, plans, and reconnection codes.
- The current dashboard is an operator UI. A multi-customer deployment requires identity-aware authentication and tenant-scoped authorization for every query.
- Agent tokens and enrollment/reconnection codes are stored only as digests by the API. Production Collector authentication should be upgraded to managed certificate/mTLS identity, rotation, and revocation.
- Recommendations, review decisions, readiness, and drift are planning evidence; the app does not execute migrations.

## Repository map

| Path | Purpose |
| --- | --- |
| `collector_agent/` | Source setup, inventory connectors, IPFIX listener, cloud flow-log readers, and local UI/API. |
| `control_plane/` | FastAPI API, database models/migrations, reporting, planning, drift, exports, and dashboard. |
| `tests/` | API, runtime, source adapter, flow-log, IPFIX, and report regression tests. |
| `CORE_PROCESS_FLOW.md` | Detailed implemented end-to-end process flow. |
| `.env.example` | Non-secret deployment configuration template. |
