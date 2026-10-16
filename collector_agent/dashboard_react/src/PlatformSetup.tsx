import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  configureAws, configureAwsFlowLogs, configureAzure, configureAzureFlowLogs, configureVmware,
  getIpfixSetup, startIpfix, stopIpfix, syncAwsFlowLogs, syncAzureFlowLogs, syncInventory,
  type IpfixSetup, type SourceType,
} from "./api/collector";
import type { CollectorSource } from "./types";
import { formatTimestamp } from "./time";

type Operation = "credentials" | "inventory" | "flow-location" | "flow-sync" | "ipfix" | null;
type Run = (operation: Operation, action: () => Promise<string>) => Promise<void>;
const readLines = (value: string) => value.split("\n").map((item) => item.trim()).filter(Boolean);
const Metric = ({ label, value }: { label: string; value: string }) => <div className="mini-metric"><span>{label}</span><strong>{value}</strong></div>;

function InventoryButton({ collectorId, operation, run, label }: { collectorId: string; operation: Operation; run: Run; label: string }) {
  return <button className="button button--primary" type="button" disabled={operation !== null} onClick={() => void run("inventory", async () => {
    const result = await syncInventory(collectorId);
    return `Inventory synchronized: ${result.discovered} discovered, ${result.accepted} accepted by the Control Plane.`;
  })}>{operation === "inventory" ? "Synchronizing..." : label}</button>;
}

export function PlatformSetup({ sourceType, sources, onComplete }: { sourceType: SourceType; sources: CollectorSource[]; onComplete: () => Promise<void> }) {
  const matching = useMemo(() => sources.filter((source) => source.source_type === sourceType), [sources, sourceType]);
  const [collectorId, setCollectorId] = useState("");
  const [operation, setOperation] = useState<Operation>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const selectedCollectorId = matching.some((source) => source.collector_id === collectorId)
    ? collectorId
    : (matching[0]?.collector_id ?? "");
  useEffect(() => {
    if (collectorId !== selectedCollectorId) setCollectorId(selectedCollectorId);
  }, [collectorId, selectedCollectorId]);
  const source = matching.find((item) => item.collector_id === selectedCollectorId);
  const run: Run = async (next, action) => {
    setOperation(next); setNotice(null); setError(null);
    try { setNotice(await action()); await onComplete(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Collector request failed."); }
    finally { setOperation(null); }
  };
  const title = sourceType === "vmware" ? "VMware setup" : sourceType === "azure" ? "Azure setup" : "AWS setup";
  const absentMessage = sourceType === "vmware" ? "Register or reconnect a collection source before configuring VMware." : `Register or reconnect an ${sourceType === "azure" ? "Azure" : "AWS"} source before configuring ${sourceType === "azure" ? "Azure" : "AWS"} collection.`;
  return <main className="main-content">
    <header className="page-header"><div><p className="eyebrow">React dashboard - Collector setup</p><h1>{title}</h1><p>Configure local collection and synchronize source data.</p></div></header>
    {notice && <div className="success-banner" role="status">{notice}</div>}{error && <div className="error-banner" role="alert">{error}</div>}
    {matching.length === 0 ? <section className="content-card"><p className="empty-state">{absentMessage}</p></section> : <>
      <section className="content-card source-selector"><label>Collection source<select value={selectedCollectorId} onChange={(event) => setCollectorId(event.target.value)}>{matching.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} - {item.collector_id}</option>)}</select></label><p>Selected source inventory in local agent: <strong>{source?.last_inventory_vm_count ?? 0} VM(s)</strong></p></section>
      {sourceType === "vmware" && <Vmware collectorId={selectedCollectorId} operation={operation} run={run} />}
      {sourceType === "azure" && <Azure collectorId={selectedCollectorId} source={source} operation={operation} run={run} />}
      {sourceType === "aws" && <Aws collectorId={selectedCollectorId} source={source} operation={operation} run={run} />}
    </>}
  </main>;
}

function Vmware({ collectorId, operation, run }: { collectorId: string; operation: Operation; run: Run }) {
  const [form, setForm] = useState({ host: "", username: "", password: "", verify: true });
  const [ipfix, setIpfix] = useState<IpfixSetup | null>(null);
  const [ipfixError, setIpfixError] = useState<string | null>(null);
  const [clusters, setClusters] = useState<string[]>([]);
  const [manual, setManual] = useState("");
  const [includedHosts, setIncludedHosts] = useState<Record<string, boolean>>({});
  const ipfixRequestSequence = useRef(0);
  const candidateKey = (candidate: IpfixSetup["exporter_candidates"][number]) => `${candidate.cluster ?? ""}::${candidate.host_name}::${candidate.vmk0_ips.join(",")}`;
  const load = async () => {
    const requestSequence = ipfixRequestSequence.current + 1;
    ipfixRequestSequence.current = requestSequence;
    try {
      const setup = await getIpfixSetup(collectorId);
      if (requestSequence !== ipfixRequestSequence.current) return;
      const availableClusters = [...new Set(setup.exporter_candidates.map((item) => item.cluster || "Unclustered"))];
      setIpfix(setup); setIpfixError(null);
      setClusters((current) => current.length === 0 ? availableClusters : current.filter((cluster) => availableClusters.includes(cluster)));
      setIncludedHosts((current) => Object.fromEntries(setup.exporter_candidates.map((candidate) => [candidateKey(candidate), current[candidateKey(candidate)] ?? true])));
    } catch (reason) {
      if (requestSequence !== ipfixRequestSequence.current) return;
      setIpfix(null); setIpfixError(reason instanceof Error ? reason.message : "Unable to load IPFIX setup.");
    }
  };
  useEffect(() => {
    setClusters([]); setIncludedHosts({}); void load();
    return () => { ipfixRequestSequence.current += 1; };
  }, [collectorId]);
  useEffect(() => { const interval = window.setInterval(() => { if (document.visibilityState === "visible") void load(); }, 15_000); return () => window.clearInterval(interval); }, [collectorId]);
  const save = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void run("credentials", async () => { await configureVmware({ collector_id: collectorId, vcenter_host: form.host, vcenter_username: form.username, vcenter_password: form.password, verify_tls: form.verify }); return "vCenter credentials are held in collector memory for this source."; }).finally(() => setForm((current) => ({ ...current, password: "" }))); };
  return <div className="platform-grid">
    <section className="content-card"><p className="eyebrow">vCenter</p><h2>Credentials</h2><p className="form-intro">Credentials are retained only in memory for the selected source and are never sent to the Control Plane.</p><form className="setup-form" onSubmit={save}><label>vCenter FQDN or IP<input required value={form.host} onChange={(event) => setForm({ ...form, host: event.target.value })} /></label><label>vCenter username<input required value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} /></label><label>vCenter password<input required type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} autoComplete="new-password" /></label><label className="confirmation"><input type="checkbox" checked={form.verify} onChange={(event) => setForm({ ...form, verify: event.target.checked })} /> Verify vCenter TLS certificate</label><button className="button button--secondary" disabled={operation !== null}>{operation === "credentials" ? "Saving..." : "Store credentials in memory"}</button></form></section>
    <section className="content-card"><p className="eyebrow">Inventory</p><h2>Synchronize inventory</h2><p className="form-intro">Refresh vCenter VM inventory and IPFIX candidates before starting collection.</p><InventoryButton collectorId={collectorId} operation={operation} run={run} label="Sync vCenter inventory" /></section>
    <IpfixSetup collectorId={collectorId} operation={operation} run={run} ipfix={ipfix} error={ipfixError} load={load} clusters={clusters} setClusters={setClusters} manual={manual} setManual={setManual} includedHosts={includedHosts} setIncludedHosts={setIncludedHosts} candidateKey={candidateKey} />
  </div>;
}

function IpfixSetup({ collectorId, operation, run, ipfix, error, load, clusters, setClusters, manual, setManual, includedHosts, setIncludedHosts, candidateKey }: { collectorId: string; operation: Operation; run: Run; ipfix: IpfixSetup | null; error: string | null; load: () => Promise<void>; clusters: string[]; setClusters: (value: string[]) => void; manual: string; setManual: (value: string) => void; includedHosts: Record<string, boolean>; setIncludedHosts: (value: Record<string, boolean>) => void; candidateKey: (candidate: IpfixSetup["exporter_candidates"][number]) => string }) {
  if (!ipfix) return <section className="content-card platform-grid__wide"><p className="eyebrow">IPFIX</p><h2>IPFIX setup</h2><div className="error-banner" role="alert">{error ?? "Unable to load IPFIX setup."} <button className="inline-button" type="button" onClick={() => void load()}>Retry</button></div></section>;
  const clusterOptions = [...new Set(ipfix.exporter_candidates.map((item) => item.cluster || "Unclustered"))];
  const includedCandidates = ipfix.exporter_candidates.filter((candidate) => clusters.includes(candidate.cluster || "Unclustered"));
  const excludedExporters = includedCandidates.flatMap((candidate) => includedHosts[candidateKey(candidate)] === false ? candidate.vmk0_ips : []);
  const running = Boolean(ipfix.selected_exporters.length);
  // Detailed local diagnostics remain available in the Streamlit troubleshooting UI.
  const showInventoryMatchDiagnostics = false as boolean;
  const matching = showInventoryMatchDiagnostics ? ipfix.matching : undefined;
  return <section className="content-card platform-grid__wide"><p className="eyebrow">IPFIX</p><h2>IPFIX setup</h2><p className={`listener-state ${running ? "listener-state--running" : "listener-state--stopped"}`}>{running ? "IPFIX listener is running." : "IPFIX listener is stopped."}</p><p className="form-intro">IPFIX counters refresh automatically every 15 seconds.</p><div className="button-row"><button className="button button--secondary" type="button" disabled={operation !== null} onClick={() => void load()}>Refresh IPFIX status</button></div><div className="metric-grid metric-grid--compact"><Metric label="Datagrams accepted" value={String(ipfix.listener.datagrams_received)} /><Metric label="Selected exporters" value={String(ipfix.selected_exporters.length)} /><Metric label="Flows found" value={String(ipfix.listener.flows_received)} /><Metric label="Observations uploaded" value={String(ipfix.listener.observations_stored)} /></div>{ipfix.listener.last_received_at && <p className="form-intro">Last datagram for this source: {formatTimestamp(ipfix.listener.last_received_at)}</p>}{ipfix.last_error && <p className="warning-text">{ipfix.last_error}</p>}{matching && <><h2>IPFIX inventory-match diagnostics</h2><p className="form-intro">Local-only diagnostics compare IPFIX source IPv4 addresses with powered-on vCenter inventory. At most 20 unmatched source/destination pairs are retained in memory.</p><div className="metric-grid metric-grid--compact"><Metric label="Powered-on inventory IPv4 addresses" value={String(matching.powered_on_inventory_ipv4_addresses)} /><Metric label="Unmatched flow records" value={String(matching.unmatched_flow_records)} /><Metric label="Last inventory-matched flow" value={matching.last_inventory_matched_flow_at ? formatTimestamp(matching.last_inventory_matched_flow_at) : "None"} /></div>{matching.unmatched_flow_samples.length > 0 && <><p className="warning-text">IPFIX flows are arriving with source IPs outside the current powered-on vCenter inventory mapping.</p><div className="table-scroll" tabIndex={0} aria-label="Unmatched IPFIX flow diagnostics"><table><thead><tr><th>Unmatched source IP</th><th>Destination IP</th><th>Flow records</th><th>Last seen</th></tr></thead><tbody>{matching.unmatched_flow_samples.map((sample) => <tr key={`${sample.source_ip}-${sample.destination_ip}`}><td>{sample.source_ip}</td><td>{sample.destination_ip}</td><td>{sample.unmatched_flow_count}</td><td>{formatTimestamp(sample.last_seen_at)}</td></tr>)}</tbody></table></div></>}</>}<p className="form-intro">One appliance listener uses UDP 4739 and routes each exporter to its assigned source.</p><div className="setup-form"><label>Clusters to trust<select multiple value={clusters} onChange={(event) => setClusters(Array.from(event.target.selectedOptions, (item) => item.value))}>{clusterOptions.map((cluster) => <option key={cluster}>{cluster}</option>)}</select></label>{includedCandidates.length > 0 ? <fieldset className="host-selection"><legend>ESXi hosts to include</legend>{includedCandidates.map((candidate) => { const key = candidateKey(candidate); return <label className="confirmation" key={key}><input type="checkbox" checked={includedHosts[key] ?? true} onChange={(event) => setIncludedHosts({ ...includedHosts, [key]: event.target.checked })} /> Include {candidate.host_name} ({candidate.cluster || "Unclustered"}) - {candidate.vmk0_ips.join(", ")}</label>; })}</fieldset> : <p className="warning-text">No vmk0 candidates are available. Synchronize inventory or enter exporter IPs.</p>}<label>Additional exporter IPs <span className="optional">(one address per line)</span><textarea value={manual} onChange={(event) => setManual(event.target.value)} /></label>{running ? <button className="button button--danger" type="button" disabled={operation !== null} onClick={() => void run("ipfix", async () => { await stopIpfix(collectorId); await load(); return "IPFIX listener stopped for this source."; })}>{operation === "ipfix" ? "Stopping..." : "Stop IPFIX for this source"}</button> : <button className="button button--primary" type="button" disabled={operation !== null} onClick={() => void run("ipfix", async () => { await startIpfix({ collector_id: collectorId, selected_clusters: clusters, manual_exporters: readLines(manual), excluded_exporters: excludedExporters }); await load(); return "IPFIX listener started on UDP 4739."; })}>{operation === "ipfix" ? "Starting..." : "Start IPFIX for this source"}</button>}</div></section>;
}

function Azure({ collectorId, source, operation, run }: { collectorId: string; source: CollectorSource | undefined; operation: Operation; run: Run }) {
  const [form, setForm] = useState({ tenant: "", client: "", secret: "", subscriptions: "" }); const [flow, setFlow] = useState({ storage: "", container: "insights-logs-flowlogflowevent", prefix: "flowLogResourceID=/" });
  const save = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void run("credentials", async () => { await configureAzure({ collector_id: collectorId, azure_tenant_id: form.tenant, azure_client_id: form.client, azure_client_secret: form.secret, subscription_ids: readLines(form.subscriptions) }); return "Azure credentials are held in collector memory for this source."; }).finally(() => setForm((current) => ({ ...current, secret: "" }))); };
  const fields = <><label>Storage account name<input required value={flow.storage} onChange={(event) => setFlow({ ...flow, storage: event.target.value })} /></label><label>Blob container<input required value={flow.container} onChange={(event) => setFlow({ ...flow, container: event.target.value })} /></label><label>Blob prefix<input required value={flow.prefix} onChange={(event) => setFlow({ ...flow, prefix: event.target.value })} /></label></>;
  return <div className="platform-grid"><section className="content-card"><p className="eyebrow">Microsoft Entra</p><h2>Application credentials</h2><p className="form-intro">Azure application credentials are retained only in memory for the selected source. They are never uploaded to the OCI Control Plane.</p><form className="setup-form" onSubmit={save}><label>Microsoft Entra tenant ID<input required value={form.tenant} onChange={(event) => setForm({ ...form, tenant: event.target.value })} /></label><label>Application (client) ID<input required value={form.client} onChange={(event) => setForm({ ...form, client: event.target.value })} /></label><label>Client secret value<input required type="password" value={form.secret} onChange={(event) => setForm({ ...form, secret: event.target.value })} autoComplete="new-password" /></label><label>Azure subscription IDs <span className="optional">(one per line)</span><textarea required value={form.subscriptions} onChange={(event) => setForm({ ...form, subscriptions: event.target.value })} /></label><button className="button button--secondary" disabled={operation !== null}>{operation === "credentials" ? "Saving..." : "Store credentials in memory"}</button></form></section><section className="content-card"><p className="eyebrow">Inventory</p><h2>Synchronize inventory</h2><p className="form-intro">The Azure connector inventories virtual machines and their private NIC IPs.</p><InventoryButton collectorId={collectorId} operation={operation} run={run} label="Sync Azure inventory" /></section><FlowLogs provider="Azure Virtual Network" configured={source?.azure_flow_logs_configured} status={source?.azure_flow_log_status} objects={source?.azure_flow_log_objects_processed} observations={source?.azure_flow_log_observations_uploaded} lastSync={source?.azure_flow_log_last_sync_at} operation={operation} fields={fields} run={run} configure={() => configureAzureFlowLogs({ collector_id: collectorId, storage_account_name: flow.storage, container_name: flow.container, prefix: flow.prefix })} sync={() => syncAzureFlowLogs(collectorId)} /></div>;
}

function Aws({ collectorId, source, operation, run }: { collectorId: string; source: CollectorSource | undefined; operation: Operation; run: Run }) {
  const [form, setForm] = useState({ region: "", access: "", secret: "", session: "" }); const [flow, setFlow] = useState({ bucket: "", prefix: "oci-migration/" });
  const save = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void run("credentials", async () => { await configureAws({ collector_id: collectorId, aws_region: form.region, aws_access_key_id: form.access || null, aws_secret_access_key: form.secret || null, aws_session_token: form.session || null }); return "AWS authentication configuration is held in collector memory for this source."; }).finally(() => setForm((current) => ({ ...current, secret: "", session: "" }))); };
  const fields = <><label>S3 bucket name<input required value={flow.bucket} onChange={(event) => setFlow({ ...flow, bucket: event.target.value })} /></label><label>S3 prefix<input required value={flow.prefix} onChange={(event) => setFlow({ ...flow, prefix: event.target.value })} /></label></>;
  return <div className="platform-grid"><section className="content-card"><p className="eyebrow">AWS authentication</p><h2>Credentials</h2><p className="form-intro">AWS credentials are retained only in memory for the selected source. Leave credential fields empty only when the appliance has an AWS SDK credential provider configured, such as IAM Roles Anywhere.</p><form className="setup-form" onSubmit={save}><label>AWS Region<input required placeholder="ap-southeast-2" value={form.region} onChange={(event) => setForm({ ...form, region: event.target.value })} /></label><label>AWS access key ID <span className="optional">(optional)</span><input value={form.access} onChange={(event) => setForm({ ...form, access: event.target.value })} /></label><label>AWS secret access key <span className="optional">(optional)</span><input type="password" value={form.secret} onChange={(event) => setForm({ ...form, secret: event.target.value })} autoComplete="new-password" /></label><label>AWS session token <span className="optional">(optional)</span><input type="password" value={form.session} onChange={(event) => setForm({ ...form, session: event.target.value })} autoComplete="new-password" /></label><button className="button button--secondary" disabled={operation !== null}>{operation === "credentials" ? "Saving..." : "Store credentials in memory"}</button></form></section><section className="content-card"><p className="eyebrow">Inventory</p><h2>Synchronize inventory</h2><p className="form-intro">The AWS connector inventories EC2 instances and their private interface IPs in the selected Region.</p><InventoryButton collectorId={collectorId} operation={operation} run={run} label="Sync AWS inventory" /></section><FlowLogs provider="AWS VPC" configured={source?.aws_flow_logs_configured} status={source?.aws_flow_log_status} objects={source?.aws_flow_log_objects_processed} observations={source?.aws_flow_log_observations_uploaded} lastSync={source?.aws_flow_log_last_sync_at} operation={operation} fields={fields} run={run} configure={() => configureAwsFlowLogs({ collector_id: collectorId, s3_bucket_name: flow.bucket, s3_prefix: flow.prefix })} sync={() => syncAwsFlowLogs(collectorId)} /></div>;
}

function FlowLogs({ provider, configured, status, objects, observations, lastSync, operation, fields, run, configure, sync }: { provider: string; configured?: boolean; status?: string; objects?: number; observations?: number; lastSync?: string | null; operation: Operation; fields: ReactNode; run: Run; configure: () => Promise<{ status: string }>; sync: () => Promise<{ objects_processed: number; observations_uploaded: number }> }) {
  const save = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); void run("flow-location", async () => { await configure(); return `${provider} Flow Log location is held in collector memory.`; }); };
  const isAws = provider === "AWS VPC"; const syncLabel = isAws ? "Sync VPC Flow Logs now" : "Sync Azure Virtual Network Flow Logs now";
  return <section className="content-card platform-grid__wide"><p className="eyebrow">{provider} Flow Logs</p><h2>Observed network traffic</h2><p className="form-intro">The collector reads only new customer-owned Flow Log objects. It polls automatically with the agent heartbeat and can also be refreshed now.</p><form className="setup-form" onSubmit={save}>{fields}<button className="button button--secondary" disabled={operation !== null}>{operation === "flow-location" ? "Saving..." : `Store ${provider} Flow Log location in memory`}</button></form>{configured && <><div className="metric-grid metric-grid--compact"><Metric label={isAws ? "Objects processed" : "Blob versions processed"} value={String(objects ?? 0)} /><Metric label="Observations uploaded" value={String(observations ?? 0)} /><Metric label="Flow Log status" value={status ?? "Waiting"} /></div>{lastSync && <p className="form-intro">Last {provider} Flow Log sync: {formatTimestamp(lastSync)}</p>}<button className="button button--primary" type="button" disabled={operation !== null} onClick={() => void run("flow-sync", async () => { const result = await sync(); return `${provider} Flow Logs synchronized: ${result.objects_processed} object(s) processed, ${result.observations_uploaded} observation(s) uploaded.`; })}>{operation === "flow-sync" ? "Synchronizing..." : syncLabel}</button></>}</section>;
}
