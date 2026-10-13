# Migration Discovery — Current Core Process Flow

This document describes the behavior implemented in the repository as of 13 October 2026. It is intended as an operational and design reference for the current product, not as a future-state architecture specification.

## 1. Purpose and operating model

Migration Discovery builds evidence for workload-migration planning. It discovers virtual-machine inventory from VMware, AWS, and Azure; collects network-flow metadata; shows the current and historical estate; recommends initial migration waves; and supports planner review, versioned plans, exports, and drift detection.

The solution has two trust zones:

```text
Customer environment                                      Control-plane environment
--------------------                                      -------------------------
VMware / AWS / Azure APIs ─┐
Flow exporters / log store ├─> Collector Agent ─HTTPS─> Control-plane API ─> PostgreSQL
Local credentials          ┘                                 │
                                                            Streamlit dashboard
```

The Collector Agent is customer-side. The Control Plane can be OCI-hosted, deployed against a customer-managed external PostgreSQL database, or used in local development. In all cases, the Control Plane stores normalized inventory and connection metadata rather than source-platform credentials or packet payloads.

## 2. Roles and responsibilities

| Role or component | Main responsibility |
| --- | --- |
| Control-plane administrator | Creates enrollment and reconnection codes; can delete a collector and its retained Control Plane data. |
| Collector operator | Registers/configures a customer-side collector, synchronizes source inventory, configures IPFIX or cloud flow-log access, and monitors collection health. |
| Migration planner | Reviews recommendations and dependencies, creates/edits plan drafts, assigns or excludes VMs, approves plans, reviews drift, and exports reports. |
| Reviewer/application owner | Reviews external, shared-service, and unavailable-internal dependencies and records the appropriate planner decision and note. |
| Collector Agent | Maintains in-memory source configuration and credentials, discovers inventory, normalizes flows, and sends authenticated metadata to the API. |
| Control Plane | Authenticates uploads, deduplicates batches, retains inventory/connection evidence, derives waves and readiness, stores plans, and creates reports. |

## 3. Security and data boundaries

### 3.1 Data that remains customer-side

- vCenter, AWS, Azure, Linux, and Windows credentials.
- Cloud flow-log locations, access keys/client secrets, and raw flow-log objects.
- Raw IPFIX datagrams and packet payloads. The product does not collect packet payloads.
- The Collector Agent's in-memory source profiles and local flow-log progress state.

The current implementation keeps Collector identities and source credentials in memory. After a Collector restart, the source must be registered/reconnected and configured again. Durable local secret storage is not implemented.

### 3.2 Data retained by the Control Plane

- Collector identity, tenant ID, display name, software version, health timestamps, and a digest—not plaintext—of the agent token.
- Inventory snapshots and append-only inventory-state evidence: VM ID, name, hostname where available, IP addresses, platform metadata, power state, and lifecycle state.
- Normalized observations: source VM ID/IP, destination IP/port, protocol, timestamp, collector type, and optional process field.
- Dependency-review decisions/notes.
- Versioned plan assignments and frozen dependency baselines at approval.

### 3.3 Connectivity

- Collector to vCenter: TCP 443.
- VMware IPFIX exporter to Collector: UDP 4739, restricted to explicitly trusted exporter addresses.
- Collector to Control Plane: outbound HTTPS in production. No OCI/Control Plane component needs inbound connectivity into the customer network.
- AWS/Azure cloud API and flow-log-store connectivity is initiated by the Collector.

## 4. Enrollment, registration, and reconnection

### 4.1 Initial enrollment

1. An administrator creates a short-lived, single-use enrollment code in the Control Plane dashboard.
2. The API stores only a SHA-256 digest, expiry time, and tenant scope for that code.
3. The collector operator enters the code, target Control Plane URL, tenant ID, display name, and source type in the loopback-only Collector setup UI.
4. The Collector calls the registration API.
5. The API validates the code, creates the Collector record, and returns the collector ID plus a bearer agent token once.
6. The Collector retains the returned token only in memory, initializes independent inventory and observation sequence counters, and starts its heartbeat loop.

### 4.2 Reconnection

1. An administrator generates a single-use reconnection code for an existing Collector.
2. The operator supplies the collector ID and reconnection code after a Collector restart or loss of its in-memory identity.
3. The API validates the code, rotates the agent token, and returns sequence values based on the highest batches already stored.
4. Existing inventory, observations, plans, and history remain associated with the same collector ID.

### 4.3 Authentication and idempotency

- Agent calls contain the collector ID and bearer agent token; the API compares a digest of the presented token.
- Inventory uploads are idempotent per `(collector_id, sequence)`.
- Observation uploads are idempotent per `(collector_id, sequence)`.
- A duplicate batch is acknowledged without duplicating retained data.
- If a local Collector restarts with stale sequence values, its runtime retries an inventory upload once with a fresh sequence.

## 5. Source inventory discovery

Each registered source is represented by its own Collector identity. A single appliance can host several source profiles, but a VMware IPFIX exporter address can belong to only one source profile on that appliance.

### 5.1 VMware

1. The operator enters vCenter connection settings in the Collector.
2. The Collector queries vCenter for VMs and normalizes VM UUID, name, hostname/IPs where returned, power state, cluster, folder, and operating-system information.
3. Duplicate paths exposing the same UUID are reduced to one deterministic VM record.
4. The complete snapshot is uploaded to the Control Plane.
5. The Collector also discovers candidate ESXi management/exporter addresses for IPFIX setup.

### 5.2 AWS

1. The operator creates an AWS source for one AWS Region.
2. The Collector uses a supplied temporary access-key pair or an available AWS SDK credential provider such as IAM Roles Anywhere.
3. It calls EC2 inventory APIs, principally `ec2:DescribeInstances`.
4. It normalizes instance ID, instance state, private interface IPs, VPC ID, tags, and Region into the common VM inventory model.
5. A separate source is required for each AWS Region.

### 5.3 Azure

1. The operator configures a Microsoft Entra tenant ID, application/client ID, client secret, and one or more subscription IDs.
2. The Collector uses Azure Resource Graph to discover `Microsoft.Compute/virtualMachines`.
3. It resolves NIC information to obtain private IP addresses and normalizes the results to the common VM inventory model.
4. Inventory is uploaded using that Azure source's collector ID.

## 6. Inventory upload, lifecycle, and historical evidence

### 6.1 Successful inventory snapshot

For every accepted non-empty snapshot, the Control Plane:

1. Creates an `inventory_batches` idempotency marker.
2. Inserts or updates the current `inventory_vms` row for each reported VM.
3. Sets the VM to `active`, clears `missing_since`, resets removal counters, and records the current source power state.
4. Appends an `inventory_vm_states` record. This is the historical evidence used by date-bounded views.
5. Updates `collectors.last_successful_inventory_at`.

An empty snapshot is rejected when trusted inventory already exists. This prevents a source/API failure from erasing the prior estate.

### 6.2 Missing and removed VMs

When a VM previously in the active/missing set does not appear in a later non-empty snapshot:

| Condition | Current lifecycle value | Current active flag | Result |
| --- | --- | --- | --- |
| First absence, or absent for less than 24 hours | `missing` | false | Retained as historical/current-status context, but not eligible for a current migration wave. |
| Still absent for at least 24 hours | `inactive` | false | Treated as removed from the current inventory; an inactive inventory-state record is appended. |
| Appears again | `active` | true | Restored to current inventory and a new active state record is appended. |

The record is retained rather than deleted. This is why historical reporting can still explain traffic and former wave membership after a VM has been powered off or removed.

### 6.3 Automatic timing

- Collector heartbeat: every 60 seconds by default.
- Inventory synchronization: every 24 hours by default (`INVENTORY_SYNC_INTERVAL_SECONDS=86400`). The supported configured range is 5 minutes through 7 days.
- The Collector retries an inventory sync after the next heartbeat when a sync fails.

## 7. Observed-network-traffic collection

All collectors normalize accepted traffic into the same observation shape:

```text
source VM UUID, source IP, destination IP, destination port,
protocol (TCP/UDP), observation timestamp, collector type, optional process
```

Only records whose source IP maps to a powered-on inventory VM in that Collector source are uploaded. Unknown sources are discarded, preventing unrelated flow records from crossing a collector/source boundary.

### 7.1 VMware IPFIX

1. The operator selects trusted ESXi/NSX/vDS exporter addresses or enters explicit exporter addresses.
2. The shared listener binds UDP 4739 and accepts datagrams only from the allowlisted addresses.
3. The listener parses supported IPFIX flow records as they arrive—there is no five-minute polling cycle.
4. The runtime assigns each exporter to its one permitted Collector source, maps a flow source IP to the current powered-on inventory map, and uploads accepted normalized flows immediately in batches.
5. UDP and TCP remain visible in reporting. Only TCP is used for automatic migration-wave grouping.

### 7.2 AWS VPC Flow Logs

1. The operator configures an S3 bucket and prefix after inventory synchronization.
2. The Collector polls the configured prefix every five minutes and offers a manual sync.
3. It lists only the configured prefix and reads only supported gzip/text flow-log objects using the expected custom field order.
4. It accepts non-denied TCP/UDP records whose source IP maps to an active AWS inventory VM.
5. It discards malformed, unsupported, rejected/denied, and unknown-source records, then uploads accepted observations.

### 7.3 Azure Virtual Network Flow Logs

1. The operator enables Azure Virtual Network Flow Logs and configures storage-account name, flow-log container, and prefix.
2. The Collector polls new bounded JSON blob versions every five minutes.
3. It accepts TCP/UDP tuples that are not denied and whose source IP matches active Azure inventory.
4. It uploads the normalized observations and keeps blob location and progress locally.

### 7.4 Current versus historical traffic

- **Latest** Current Environment view begins at the latest successful inventory sync and ends at the current time. For older Collectors whose marker was introduced after they already had inventory, the API safely falls back to the newest trusted inventory-batch timestamp.
- **Historical period** uses the selected 1-day, 5-day, 1-week, 1-month, or custom date interval.
- Date ranges are inclusive in the dashboard's reporting-window semantics.

## 8. Current Environment dashboard process

The Current Environment page is an estate and evidence view, not a planning page.

1. The user selects one Collector and either **Latest** or **Historical period**.
2. The dashboard retrieves inventory, connection evidence, automatic waves, and wave summary for the same reporting boundary.
3. For a historical view, inventory comes from the latest state at or before the selected end date. A VM first discovered after the end date is not projected into the past.
4. For old Collectors with no inventory-state record, only VM identities supported by observations in that period are reconstructed; the entire retained estate is never projected backwards.
5. The dashboard displays estate counts, the automatic wave summary, a Recommended wave or Focused VM graph, a filtered inventory table, and deduplicated observed connections.

The graph uses a compact Graphviz representation:

- Internal discovered VM nodes are shown individually.
- External endpoints are collapsed into one gold node.
- At most 100 unique graph edges are drawn.
- Dynamic/private port labels in the range 32768–65535 are suppressed from graph labels, although their data remains available where applicable.
- In a historical view, a VM whose **current** state has changed has a hover tooltip. A powered-off VM is orange with a warning icon; a VM removed from inventory is red with a warning icon. The graph remains historical evidence; the colour indicates its current state, not its historical state.

The Current observed connections table is deduplicated to one logical connection per source VM, destination, protocol, and port. It shows an evidence label (`Observed once` or `Observed repeatedly`) and observation count; exact first/last times remain available in the detailed evidence/export paths. Dynamic/private TCP destination ports are omitted from this compact table to reduce ephemeral-port noise.

## 9. Automatic migration-wave recommendation

### 9.1 Eligibility

For the current recommendation, a VM must be:

1. In the selected Collector's inventory;
2. Active; and
3. In the `poweredOn` state.

For historical recommendation, the same eligibility is evaluated from the retained inventory state during the chosen period. An old historical period before a VM's first discovery must not include that VM.

### 9.2 Grouping algorithm

1. Start with every eligible VM as a candidate, including VMs that may eventually become singleton waves.
2. Inspect observations inside the selected reporting window.
3. Ignore all non-TCP traffic for grouping.
4. Map each TCP destination IP to an eligible inventory VM in the same Collector.
5. Add an undirected relationship between the source and destination only when both are eligible, internal VMs and they are not the same VM.
6. Calculate connected components of that internal TCP graph.
7. Each connected component becomes one recommended wave. Components are sorted primarily by descending member count, then deterministically by VM identity.

Consequences:

- A brief but valid internal TCP observation can connect two VMs for the selected reporting window.
- Selecting a larger historical window increases the evidence considered; it does not invent connections outside that window.
- An internal TCP dependency on a powered-off/missing/removed VM does not merge it into the current wave. It is retained as an unavailable internal dependency for review.
- External destinations never merge waves.
- UDP—including DNS, NTP, DHCP, and DHCPv6—never merges waves. It remains operational reporting evidence.
- A VM with no observed internal TCP relationship is a singleton recommended wave while it is eligible.

### 9.3 Recommended versus approved waves

- Automatic recommendations are recalculated from selected inventory and traffic evidence.
- When an approved plan exists, plan-centric pages use its frozen included wave assignments as the effective planning waves.
- The Current Environment page explicitly uses automatic discovery, not an approved plan, so planners can see the current/historical technical estate independently of a plan.

## 10. Dependency Review process

Dependency Review converts non-grouping or exceptional traffic into reviewable items. It normalizes relationships by source, destination identity, protocol, port/service key, and category, and attaches occurrence evidence.

### 10.1 Categories

| Category | Meaning | Effect on automatic grouping |
| --- | --- | --- |
| External dependency | Destination is not a known internal VM. | Never merges waves. |
| Shared infrastructure | Recognized UDP infrastructure service, such as DNS/NTP/DHCP. | Reporting-only; never merges waves. |
| Shared infrastructure candidate | Certain TCP service ports that may represent a shared service. | Does not by itself alter wave grouping; requires review. |
| Unavailable internal VM | A known internal destination is currently unavailable. | Not merged into a current wave; preserved for review. |
| Internal VM dependency | Internal VM relationship normalized for evidence/planning context. | TCP between eligible VMs is used by the automatic wave graph. |

Dynamic/private TCP ports are normalized to a `dynamic-private` key in dependency review so numerous ephemeral ports do not become numerous independent review decisions.

### 10.2 Planner decisions and readiness translation

The planner can select multiple review rows when the same decision and note apply, then save a common decision/note. Saved decisions are unique by collector and normalized dependency key.

The dashboard translates decisions into readiness statuses such as **Resolved**, **Advisory**, **Action required**, **Investigate**, and **High impact**. The Dependency Review page provides filters for All, Not reviewed, Action required, Investigate, High impact, Resolved, and related categories so planners do not have to infer status from raw decision text.

Decision notes are evidence for the reviewed dependency. They do not automatically become a plan-VM assignment note; plan notes belong to the VM assignment inside a plan draft.

## 11. Wave readiness process

Wave readiness explains evidence quality and unresolved dependency work; it is not automatic cutover authorization.

### 11.1 Confidence

Confidence is based on evidence quality/freshness for the selected window:

- **High**: observation coverage is at least 80% of the selected interval, active inventory is no more than 7 days old, the Collector checked in within 180 seconds, and at least one observation exists.
- **Low**: coverage is below 40%, inventory is more than 30 days old, or no observations exist.
- **Medium**: all other cases.

Coverage is calculated from the span between the earliest and latest observation, divided by the selected window duration, capped at 100%.

### 11.2 Risk

- **High risk**: at least one review item is high impact.
- **Medium risk / needs review**: action-required or investigate items remain.
- **Low risk**: no unresolved review dependencies remain.
- **Ready for approval**: High confidence **and** Low risk. This is a planning signal only, not cutover approval.

## 12. Migration Plan lifecycle

Plans are collector-scoped and versioned. A collector can have several historical versions but only one active draft at a time and one approved plan at a time.

### 12.1 Create a draft from current recommendation

1. The planner selects a Collector and reporting period.
2. The API calculates the automatic recommendation from the current eligible powered-on inventory and selected observations.
3. A new plan version is created only if no draft already exists for that Collector.
4. Every recommended VM becomes an included assignment with both a recommended wave number and an initial planned wave number.

### 12.2 Create a new version from an approved plan

1. The planner selects an approved source plan.
2. The API clones all frozen assignments—member, disposition, planned wave, recommended wave reference, and VM note—into the next version as a draft.
3. This is the appropriate starting point to revise a previously approved plan, including bringing back an earlier excluded VM or accommodating drift.

### 12.3 Change a draft

For one or multiple selected VMs with the same intended action, the planner can:

- Keep the VM included and assign it to an existing planned wave.
- Move it to another existing planned wave.
- Create a new planned wave for it.
- Exclude that individual VM from the plan and add a plan-VM note.

Exclusion is a **per-VM plan disposition**, not deletion of an automatic wave and not removal of technical inventory/traffic evidence.

### 12.4 Approve

1. The planner enters an approver name and types the required confirmation.
2. The draft becomes `approved` and receives an approval timestamp.
3. Any prior approved plan for the Collector becomes `superseded`.
4. The API freezes a normalized dependency baseline for the approved plan.
5. Approved plans are read-only. Further changes require a new draft version.

## 13. Plan drift detection

Drift is automatically calculated against the latest approved plan. It is informational; it neither changes nor invalidates a plan automatically.

The drift report identifies:

| Drift condition | Severity |
| --- | --- |
| An approved included VM is missing from current inventory. | Critical |
| An approved included VM is no longer active and powered on. | Critical |
| A currently eligible powered-on VM is in neither the approved included set nor approved excluded set. | Warning |
| A new reviewable dependency involving a planned VM appears after approval and was not in the frozen baseline. | Critical for unavailable internal/planned internal relationship; otherwise Warning. |

Previously excluded VMs do not generate a “new VM not in plan” warning merely because they remain eligible; the approved plan already records that explicit disposition.

Drift is displayed on the Migration Plan page and summarized in Wave Readiness. Plan reports include the selected plan's drift information when applicable.

## 14. Reporting and exports

### 14.1 Migration Waves reports

Migration Waves supports exports for the currently selected display and observation window:

- Executive PDF: concise summary, inventory/wave evidence, confidence/risk criteria, and wave/review context.
- Detailed Excel: more granular technical evidence, including normalized TCP/UDP connection details.

When displaying automatic recommendation, these exports report that recommendation. When displaying a latest approved plan, they report that selected approved plan context rather than silently replacing it with current recommendation.

### 14.2 Migration Plan reports

The Migration Plan page exports the selected plan version—not merely the newest plan—so planners can retrieve a draft, approved, or superseded version deliberately. The report identifies the plan status and includes frozen VM assignments, exclusions, planner notes, dependency information, drift detail, and TCP/UDP connection evidence appropriate to the selected plan/report.

## 15. Health, failures, and recovery behaviour

| Situation | Behaviour |
| --- | --- |
| No successful heartbeat for 180 seconds | Control Plane displays the Collector as offline. |
| Inventory API/source failure | Existing trusted inventory is retained; Collector records a local error and retries after a subsequent heartbeat. |
| Empty inventory returned while existing inventory exists | API rejects the snapshot to prevent accidental estate deletion. |
| Duplicate upload sequence | API ignores the duplicate without duplicating data. |
| IPFIX exporter is assigned to a different source on the appliance | Startup/configuration is rejected until the conflict is resolved. |
| IPFIX source IP does not map to powered-on current inventory | The flow is not uploaded as an observation. |
| AWS/Azure flow row is denied, malformed, unsupported, or source-unmatched | It is discarded before upload. |
| Collector reboot | In-memory identity/configuration is lost; use a reconnection code and reconfigure the source. Control Plane history remains. |
| Source unregistered locally | Local collection/configuration is removed and VMware exporter assignment released. Control Plane data is retained. |
| Collector deleted by administrator | Collector-scoped inventory, states, observations, batches, dependency decisions, plans, and reconnection codes are permanently removed. |

## 16. Important implementation boundaries and limitations

- Guest operating-system application profiling is not part of this implemented network-discovery flow. The codebase may contain groundwork/previous-app references, but no completed cross-platform guest profiling workflow is represented here.
- The current Streamlit dashboard is an operator UI. It does not implement end-user tenant-scoped authorization. A production multi-customer deployment requires identity-aware access control and tenant-scoped enforcement at every query.
- The current production design requires TLS verification and appropriate deployment secret handling. Development loopback HTTP/SQLite paths are explicitly development-only.
- The Collector's source credentials and agent identity are intentionally non-persistent in the current implementation.
- Migration-wave recommendations are evidence-based suggestions. They do not authorize cutover, replace application-owner validation, or automatically modify approved plans.

## 17. Source-code map

| Area | Principal files |
| --- | --- |
| Collector runtime and scheduling | `collector_agent/runtime.py`, `collector_agent/config.py` |
| VMware inventory/IPFIX | `collector_agent/vcenter.py`, `collector_agent/ipfix.py` |
| AWS inventory/flow logs | `collector_agent/aws.py`, `collector_agent/aws_flow_logs.py` |
| Azure inventory/flow logs | `collector_agent/azure.py`, `collector_agent/azure_flow_logs.py` |
| Collector setup UI/API | `collector_agent/dashboard.py`, `collector_agent/main.py`, `collector_agent/client.py` |
| Control Plane API and lifecycle | `control_plane/main.py`, `control_plane/models.py`, `control_plane/schemas.py` |
| Waves, reviews, readiness, and drift | `control_plane/reporting.py` |
| Control Plane UI | `control_plane/dashboard.py`, `control_plane/dashboard_client.py` |
| PDF/Excel exports | `control_plane/executive_report.py`, `control_plane/detailed_report.py`, `control_plane/plan_report.py` |
| Database migrations | `control_plane/migrations/versions/` |
| Behaviour regression tests | `tests/test_control_plane.py`, `tests/test_agent_runtime.py`, `tests/test_aws*.py`, `tests/test_azure*.py`, `tests/test_ipfix.py`, report tests |
