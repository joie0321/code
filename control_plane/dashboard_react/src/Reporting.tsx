import { useEffect, useMemo, useState } from "react";

import { api } from "./api";
import { formatTimestamp } from "./time";

type Row = Record<string, any>;
type ReportKind = "executive" | "technical";

const RANGES: Record<string, number> = {
  "1 day": 1,
  "5 days": 5,
  "1 week": 7,
  "1 month": 30,
};
const PAGE_SIZE = 100;

function utc(value: Date): string {
  return `${value.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

function Card({ label, value }: { label: string; value: number }) {
  return <article className="metric-card"><i /><p>{label}</p><strong>{value}</strong></article>;
}

function recommendationBasis(wave: Row): string {
  const basis = String(wave.reason ?? "Automatic recommendation");
  const warnings = Array.isArray(wave.availability_warnings) ? wave.availability_warnings : [];
  if (!warnings.length) return basis;
  const changes = warnings.flatMap((warning: Row) => (warning.members ?? []).map((member: Row) => {
    const prefix = warning.plan_review ? "Requires plan review" : `Previously in Wave ${warning.previous_wave}`;
    return `${prefix}: ${member.name} is ${member.reason}.`;
  }));
  return `${basis} Change since previous recommendation: ${changes.join(" ")}`;
}

function Table({ rows, page, setPage }: { rows: Row[]; page: number; setPage: (page: number) => void }) {
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const pageRows = rows.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const fields = pageRows[0] ? Object.keys(pageRows[0]) : [];
  if (!fields.length) return <p className="empty-state">No rows match this report selection.</p>;
  return <>
    <div className="table-scroll"><table><thead><tr>{fields.map((field) => <th key={field}>{field}</th>)}</tr></thead><tbody>
      {pageRows.map((row, index) => <tr key={index}>{fields.map((field) => <td key={field}>{String(row[field] ?? "-")}</td>)}</tr>)}
    </tbody></table></div>
    {totalPages > 1 ? <div className="pager"><button type="button" disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>Previous</button><span>Page {currentPage} of {totalPages}; showing up to {PAGE_SIZE} rows per page.</span><button type="button" disabled={currentPage === totalPages} onClick={() => setPage(currentPage + 1)}>Next</button></div> : null}
  </>;
}

export function Reporting({ collectors }: { collectors: Row[] }) {
  const [collectorId, setCollectorId] = useState("");
  const [reportKind, setReportKind] = useState<ReportKind>("executive");
  const [historical, setHistorical] = useState(false);
  const [range, setRange] = useState("1 day");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [selectedWave, setSelectedWave] = useState(0);
  const [connectionPage, setConnectionPage] = useState(1);
  const [connectionFilter, setConnectionFilter] = useState("");
  const [data, setData] = useState<{ inventory: Row[]; connections: Row[]; waves: Row[]; retiredWaves: Row[]; summary: Row; label: string; observedAfter?: string; observedBefore?: string; trafficReceivedAfter?: string }>();
  const [connectionData, setConnectionData] = useState<{ items: Row[]; total: number; page: number; page_size: number }>();
  const [error, setError] = useState("");

  useEffect(() => {
    if (!collectorId && collectors[0]?.collector_id) setCollectorId(String(collectors[0].collector_id));
  }, [collectorId, collectors]);

  const selectedCollector = useMemo(() => collectors.find((item) => String(item.collector_id) === collectorId), [collectorId, collectors]);
  // Current Inventory changes only when a successful source marker moves.
  // Historical ranges stay fixed until the operator changes them.
  const latestMarker = historical ? "historical" : `${selectedCollector?.last_successful_inventory_at ?? ""}|${selectedCollector?.last_successful_traffic_at ?? ""}`;

  useEffect(() => {
    if (!collectorId) return;
    const query = new URLSearchParams();
    let historicalAsOf: string | undefined;
    let label: string;
    if (historical) {
      let start: Date;
      let end: Date;
      if (range === "Custom time range") {
        if (!customStart || !customEnd) { setData(undefined); setError("Select both a start date and an end date to apply the custom range."); return; }
        if (customEnd < customStart) { setData(undefined); setError("The custom end date must be on or after the start date."); return; }
        start = new Date(`${customStart}T00:00:00.000Z`);
        end = new Date(`${customEnd}T23:59:59.999Z`);
        label = `${customStart} to ${customEnd} (${utc(start)} to ${utc(end)})`;
      } else {
        end = new Date();
        start = new Date(end.getTime() - RANGES[range] * 86_400_000);
        label = `${range} (${utc(start)} to ${utc(end)})`;
      }
      query.set("historical", "true");
      query.set("observed_after", start.toISOString());
      query.set("observed_before", end.toISOString());
      historicalAsOf = end.toISOString();
    } else {
      const lastInventory = selectedCollector?.last_successful_inventory_at;
      const lastTraffic = selectedCollector?.last_successful_traffic_at;
      if (typeof lastInventory !== "string" || typeof lastTraffic !== "string") {
        setData(undefined);
        setError("A successful inventory sync and traffic upload are required before the Current Inventory traffic window can be calculated.");
        return;
      }
      query.set("traffic_received_after", lastInventory);
      label = `Traffic received since the current inventory snapshot (${utc(new Date(lastInventory))} through ${utc(new Date(lastTraffic))})`;
    }
    let active = true;
    setError("");
    void Promise.all([api.inventory(collectorId, historical, historicalAsOf), api.currentEnvironment(collectorId, query)])
      .then(([inventory, environment]) => {
        if (!active) return;
        setData({ inventory, connections: environment.connections, waves: environment.waves, retiredWaves: environment.retired_waves ?? [], summary: environment.summary, label, observedAfter: query.get("observed_after") ?? undefined, observedBefore: query.get("observed_before") ?? undefined, trafficReceivedAfter: query.get("traffic_received_after") ?? undefined });
        setSelectedWave((current) => Math.min(current, Math.max(0, environment.waves.length - 1)));
        setConnectionPage(1);
      })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed."); });
    return () => { active = false; };
  }, [collectorId, customEnd, customStart, historical, latestMarker, range, refresh]);

  const selectedWaveData = data?.waves[selectedWave];
  const selectedVmIds = useMemo(() => (selectedWaveData?.server_vm_uuids ?? []).map(String), [selectedWaveData]);
  useEffect(() => {
    if (!data || !collectorId || reportKind !== "technical" || !selectedVmIds.length) { setConnectionData(undefined); return; }
    const args = new URLSearchParams({ page: String(connectionPage), page_size: "250", deduplicate: "true", exclude_dynamic_private_ports: "true" });
    // This deliberately matches Current Environment's observed-connections
    // request: same scope, filters, and server-side de-duplication behavior.
    for (const vmUuid of selectedVmIds) args.append("vm_uuid", vmUuid);
    if (data.observedAfter) args.set("observed_after", data.observedAfter);
    if (data.observedBefore) args.set("observed_before", data.observedBefore);
    if (data.trafficReceivedAfter) args.set("traffic_received_after", data.trafficReceivedAfter);
    let active = true;
    void api.waveConnections(collectorId, args)
      .then((response) => { if (active) setConnectionData(response); })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed."); });
    return () => { active = false; };
  }, [collectorId, connectionPage, data, reportKind, selectedVmIds]);

  const observedIds = useMemo(() => new Set((data?.connections ?? []).flatMap((item) => [item.source_vm_uuid, item.destination_vm_uuid].filter(Boolean).map(String))), [data]);
  const waveRows = useMemo(() => [
    ...(data?.waves ?? []).map((wave, index) => ({
      Wave: `Wave ${wave.wave ?? index + 1}`,
      "VM count": (wave.server_vm_uuids ?? []).length,
      "VMs": (wave.server_names ?? []).join(", ") || "-",
      "Recommendation basis": recommendationBasis(wave),
    })),
    ...(data?.retiredWaves ?? []).map((wave) => ({
      Wave: `Previous Wave ${wave.previous_wave} - unavailable`,
      "VM count": (wave.members ?? []).length,
      "VMs": (wave.members ?? []).map((member: Row) => member.name).join(", ") || "-",
      "Recommendation basis": `${wave.reason} ${((wave.members ?? []) as Row[]).map((member) => `${member.name} is ${member.reason}.`).join(" ")}`,
    })),
  ], [data]);
  const inventoryRows = useMemo(() => (data?.inventory ?? []).map((item) => ({
    VM: item.vm_name ?? "",
    Hostname: item.hostname ?? "",
    "IP addresses": item.ips ?? "",
    "Power state": item.power_state ?? "",
    Cluster: item.cluster ?? "",
    Folder: item.folder ?? "",
    "Operating system": item.os_name ?? "",
    "Traffic observed": observedIds.has(String(item.vm_uuid)) ? "Yes" : "No",
  })), [data, observedIds]);
  const exactConnectionRows = useMemo(() => (connectionData?.items ?? []).map((item) => ({
    "Source VM": item.source_vm_name ?? "",
    Destination: item.destination_hostname ?? item.destination_vm_name ?? item.destination_ip ?? "",
    "Destination IP": item.destination_ip ?? "",
    "Connection type": item.destination_vm_uuid ? "Internal" : "External",
    Protocol: String(item.protocol ?? "").toUpperCase(),
    Port: item.destination_port ?? "",
    "Traffic classification": item.traffic_class ?? "",
    Evidence: item.evidence ?? "",
    Observations: item.observation_count ?? "",
  })), [connectionData]);
  const filteredConnectionRows = useMemo(() => exactConnectionRows.filter((row) => !connectionFilter || Object.values(row).join(" ").toLowerCase().includes(connectionFilter.toLowerCase())), [connectionFilter, exactConnectionRows]);
  const poweredOn = (data?.inventory ?? []).filter((item) => item.power_state === "poweredOn").length;
  const poweredOff = (data?.inventory ?? []).filter((item) => item.power_state === "poweredOff").length;

  return <main className="main-content">
    <p className="eyebrow">React Control Plane</p><h1>Reports</h1>
    <p className="subtle">Prepare an executive summary or technical/planner report from the same inventory and traffic window used by Current Environment.</p>
    {!collectors.length ? <p className="empty-state">No registered collectors are available yet.</p> : <>
      <section className="filters">
        <label>Collector<select value={collectorId} onChange={(event) => setCollectorId(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} - {item.tenant_id} - {item.collector_id}</option>)}</select></label>
        <label>Report type<select value={reportKind} onChange={(event) => setReportKind(event.target.value as ReportKind)}><option value="executive">Executive report</option><option value="technical">Technical / planner report</option></select></label>
        <fieldset aria-label="Report inventory view"><label className="inventory-window-option"><input type="radio" checked={!historical} onChange={() => setHistorical(false)} />Current Inventory <span className="info-icon" tabIndex={0} role="img" aria-label="Current Inventory uses traffic accepted from the latest successful inventory sync through the latest successful traffic upload." data-tooltip="Includes traffic accepted from the latest successful inventory sync through the latest successful traffic upload. This keeps traffic aligned with the current inventory snapshot.">i</span></label><label><input type="radio" checked={historical} onChange={() => setHistorical(true)} />Historical period</label></fieldset>
        {historical ? <label>Observed connection time range<select value={range} onChange={(event) => setRange(event.target.value)}>{[...Object.keys(RANGES), "Custom time range"].map((item) => <option key={item}>{item}</option>)}</select></label> : null}
        {historical && range === "Custom time range" ? <><label>Custom start date<input type="date" value={customStart} onChange={(event) => setCustomStart(event.target.value)} /></label><label>Custom end date<input type="date" value={customEnd} onChange={(event) => setCustomEnd(event.target.value)} /></label></> : null}
        {historical ? <button className="button button--primary" type="button" onClick={() => setRefresh((value) => value + 1)}>Refresh now</button> : null}
      </section>
      {error ? <div className="error-banner" role="alert">{error}</div> : null}
      {data ? <>
        <section className="content-card report-context"><h2>Report context</h2><dl><div><dt>Inventory source</dt><dd>{selectedCollector?.display_name ?? "-"}</dd></div><div><dt>Source type</dt><dd>{selectedCollector?.source_type ?? "-"}</dd></div><div><dt>Inventory / traffic window</dt><dd>{data.label}</dd></div><div><dt>Generated</dt><dd>{formatTimestamp(new Date().toISOString())}</dd></div></dl></section>
        <h2>VM inventory summary</h2><section className="metric-grid five"><Card label="Inventory VMs" value={data.inventory.length} /><Card label="Powered-on VMs" value={poweredOn} /><Card label="Powered-off VMs" value={poweredOff} /><Card label="VMs with observed traffic" value={observedIds.size} /><Card label="VMs without observed traffic" value={Math.max(0, data.inventory.length - observedIds.size)} /></section>
        <h2>Recommended wave summary</h2><p className="subtle">Recommended waves are automatic discovery output, not an approved migration plan.</p><section className="metric-grid"><Card label="Recommended waves" value={Number(data.summary.wave_count ?? data.waves.length)} /><Card label="Waves with VM-to-VM connections" value={Number(data.summary.waves_with_observed_connections ?? 0)} /><Card label="Waves without VM-to-VM connections" value={Number(data.summary.waves_without_observed_connections ?? 0)} /></section><Table rows={waveRows} page={1} setPage={() => undefined} />
        {reportKind === "technical" ? <>
          <h2>Detailed VM inventory</h2><Table rows={inventoryRows} page={1} setPage={() => undefined} />
          <h2>Observed traffic report</h2><p className="subtle">This is the same selected-wave observed-connections table and server-side query used by Current Environment. No additional report-level aggregation or de-duplication is applied.</p>
          {data.waves.length ? <><label className="collector-select">Wave for observed traffic<select value={selectedWave} onChange={(event) => { setSelectedWave(Number(event.target.value)); setConnectionPage(1); }}>{data.waves.map((wave, index) => <option key={index} value={index}>Wave {wave.wave ?? index + 1} - {(wave.server_names ?? []).length} VM(s)</option>)}</select></label><p className="refresh-status">Showing {connectionData?.total ?? 0} unique connection(s) for the selected {historical ? "historical period" : "Current Inventory"}.</p><input className="table-filter" placeholder="Filter connections by source, destination, IP, protocol, or port" value={connectionFilter} onChange={(event) => { setConnectionFilter(event.target.value); setConnectionPage(1); }} /><Table rows={filteredConnectionRows} page={1} setPage={() => undefined} />{connectionData && Math.ceil(connectionData.total / connectionData.page_size) > 1 ? <div className="pager"><button type="button" disabled={connectionData.page <= 1} onClick={() => setConnectionPage(connectionData.page - 1)}>Previous</button><span>Page {connectionData.page} of {Math.ceil(connectionData.total / connectionData.page_size)}; showing up to {connectionData.page_size} rows per page.</span><button type="button" disabled={connectionData.page >= Math.ceil(connectionData.total / connectionData.page_size)} onClick={() => setConnectionPage(connectionData.page + 1)}>Next</button></div> : null}</> : <p className="empty-state">No recommended wave is available for this inventory and traffic window.</p>}
        </> : null}
      </> : null}
    </>}
  </main>;
}
