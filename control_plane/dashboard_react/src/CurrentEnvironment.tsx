import { useEffect, useMemo, useState } from "react";
import { Graphviz } from "@hpcc-js/wasm-graphviz";

import { api } from "./api";

type Row = Record<string, any>;

const RANGES: Record<string, number> = {
  "1 day": 1,
  "5 days": 5,
  "1 week": 7,
  "1 month": 30,
};
const PAGE_SIZE = 100;

function dotText(value: unknown): string {
  return String(value ?? "").replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/[\r\n]+/g, " ");
}

function compactLabel(value: unknown, maximum = 16): string {
  const text = String(value ?? "");
  return text.length > maximum ? `${text.slice(0, maximum - 1)}…` : text;
}

function portLabel(values: Set<string>): string {
  const labels = [...values].sort();
  return labels.length > 3 ? `${labels.slice(0, 3).join(", ")}, …` : labels.join(", ");
}

function vmStatus(item: Row): string {
  if (!item.current_is_active && item.current_is_active !== undefined) return "removed from inventory";
  if (!item.is_active && item.is_active !== undefined) return "removed from inventory";
  const powerState = item.current_power_state ?? item.power_state;
  return powerState && powerState !== "poweredOn" ? "powered off" : "active and powered on";
}

function inventoryStatus(item: Row): string {
  if (item.current_is_active ?? item.is_active) return "Current";
  return (item.current_lifecycle_status ?? item.lifecycle_status) === "missing"
    ? "Missing from latest inventory"
    : "Removed from current inventory";
}

function utc(value: Date): string {
  return `${value.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

function GraphvizGraph({ inventory, connections, focusVmUuid, zoom }: {
  inventory: Row[]; connections: Row[]; focusVmUuid?: string; zoom: number;
}) {
  const [svg, setSvg] = useState("");
  const [renderError, setRenderError] = useState("");
  const dot = useMemo(() => {
    const nodes = new Map(inventory.map((item, index) => [String(item.vm_uuid), `vm_${index + 1}`]));
    const edgePorts = new Map<string, Set<string>>();
    const externalDestinations = new Set<string>();
    for (const connection of connections) {
      const source = nodes.get(String(connection.source_vm_uuid ?? ""));
      if (!source) continue;
      let destination: string | undefined;
      if (connection.destination_vm_uuid) {
        destination = nodes.get(String(connection.destination_vm_uuid));
        if (!destination) continue;
      } else {
        destination = "external_endpoints";
        externalDestinations.add(String(connection.destination_ip ?? "Unknown"));
      }
      const edge = `${source} -> ${destination}`;
      if (!edgePorts.has(edge)) edgePorts.set(edge, new Set<string>());
      const port = Number(connection.destination_port);
      const protocol = String(connection.protocol ?? "tcp");
      edgePorts.get(edge)?.add(
        connection.approved_dynamic_port || port < 32768 || port > 65535
          ? `${protocol}/${connection.destination_port ?? ""}`
          : protocol,
      );
      if (edgePorts.size >= 100) break;
    }
    const scale = zoom / 100;
    const lines = [
      "digraph dependency_map {", "rankdir=LR;",
      'graph [bgcolor="transparent", pad="0.18", nodesep="0.42", ranksep="0.85", splines="polyline"];',
      `node [shape=box, style="rounded,filled", fontname="Arial", fontsize=${(9 * scale).toFixed(1)}, margin="0.06,0.04", width=${(1.65 * scale).toFixed(2)}, height=${(0.45 * scale).toFixed(2)}, fixedsize=true, color="#64748b", fillcolor="#e2e8f0", fontcolor="#0f172a", penwidth=1.1];`,
      `edge [fontname="Arial", fontsize=${(8 * scale).toFixed(1)}, color="#64748b", fontcolor="#94a3b8", arrowsize=${(0.65 * scale).toFixed(2)}, penwidth=1.0];`,
    ];
    for (const item of inventory) {
      const id = nodes.get(String(item.vm_uuid));
      if (!id) continue;
      const status = vmStatus(item);
      let label = dotText(compactLabel(item.vm_name ?? item.vm_uuid));
      const attributes = [`label="${label}"`, `tooltip="${dotText(`${item.vm_name ?? item.vm_uuid}\nCurrent status: ${status}`)}"`];
      if (status === "powered off") {
        label = `⚠ ${label}`;
        attributes[0] = `label="${label}"`;
        attributes.push('style="rounded,dashed,filled"', 'color="#c2410c"', 'fillcolor="#ffedd5"', 'fontcolor="#7c2d12"');
      } else if (status === "removed from inventory") {
        label = `⚠ ${label}`;
        attributes[0] = `label="${label}"`;
        attributes.push('style="rounded,dashed,filled"', 'color="#b91c1c"', 'fillcolor="#fee2e2"', 'fontcolor="#7f1d1d"');
      } else if (String(item.vm_uuid) === focusVmUuid) {
        attributes.push('color="#2563eb"', 'fillcolor="#dbeafe"', "penwidth=2.0");
      }
      lines.push(`${id} [${attributes.join(", ")}];`);
    }
    if (externalDestinations.size) lines.push(`external_endpoints [label="External endpoints\\n${externalDestinations.size} destination(s)", color="#a47527", fillcolor="#fef3c7", fontcolor="#3b2f10"];`);
    for (const [edge, ports] of edgePorts) lines.push(`${edge} [label="${dotText(portLabel(ports))}"];`);
    lines.push("}");
    return lines.join("\n");
  }, [connections, focusVmUuid, inventory, zoom]);

  useEffect(() => {
    let alive = true;
    setSvg(""); setRenderError("");
    void Graphviz.load().then((renderer) => renderer.layout(dot, "svg", "dot")).then((rendered) => {
      if (alive) setSvg(rendered);
    }).catch(() => {
      if (alive) setRenderError("Unable to render the Graphviz dependency graph.");
    });
    return () => { alive = false; };
  }, [dot]);
  if (!inventory.length) return <p className="empty-state">No discovered VMs are available to draw for this selection.</p>;
  if (renderError) return <p className="error-banner">{renderError}</p>;
  return <div className="graphviz-graph" aria-label="Dependency graph" dangerouslySetInnerHTML={{ __html: svg }} />;
}

function PageTable({ rows, page, setPage }: { rows: Row[]; page: number; setPage: (page: number) => void }) {
  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const pageRows = rows.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const fields = pageRows[0] ? Object.keys(pageRows[0]) : [];
  if (!fields.length) return <p className="empty-state">No rows match this selection.</p>;
  return <>
    <div className="table-scroll"><table><thead><tr>{fields.map((field) => <th key={field}>{field}</th>)}</tr></thead><tbody>
      {pageRows.map((row, index) => <tr key={index}>{fields.map((field) => <td key={field}>{String(row[field] ?? "—")}</td>)}</tr>)}
    </tbody></table></div>
    {totalPages > 1 ? <div className="pager"><button disabled={currentPage === 1} onClick={() => setPage(currentPage - 1)}>Previous</button><span>Page {currentPage} of {totalPages}; showing up to {PAGE_SIZE} rows per page.</span><button disabled={currentPage === totalPages} onClick={() => setPage(currentPage + 1)}>Next</button></div> : null}
  </>;
}

function Card({ label, value }: { label: string; value: number }) {
  return <article className="metric-card"><i /><p>{label}</p><strong>{value}</strong></article>;
}

export function CurrentEnvironment({ collectors }: { collectors: Row[] }) {
  const [collectorId, setCollectorId] = useState("");
  const [historical, setHistorical] = useState(false);
  const [range, setRange] = useState("1 day");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [data, setData] = useState<{ inventory: Row[]; connections: Row[]; waves: Row[]; summary: Row; label: string; observedAfter?: string; observedBefore?: string; trafficReceivedAfter?: string }>();
  const [connectionPageData, setConnectionPageData] = useState<{ items: Row[]; total: number; page: number; page_size: number }>();
  const [mapView, setMapView] = useState<"Recommended wave" | "Focused VM">("Recommended wave");
  const [selectedWave, setSelectedWave] = useState(0);
  const [focusVm, setFocusVm] = useState("");
  const [zoom, setZoom] = useState(100);
  const [inventoryFilter, setInventoryFilter] = useState("");
  const [connectionFilter, setConnectionFilter] = useState("");
  const [inventoryPage, setInventoryPage] = useState(1);
  const [connectionPage, setConnectionPage] = useState(1);
  const [manualRefresh, setManualRefresh] = useState(0);
  const [error, setError] = useState("");
  const [allowlistPorts, setAllowlistPorts] = useState<{ protocol: "tcp" | "udp"; port: number }[]>([]);
  const [allowlistMessage, setAllowlistMessage] = useState("");
  const [newAllowlistProtocol, setNewAllowlistProtocol] = useState<"tcp" | "udp">("tcp");
  const [newAllowlistPort, setNewAllowlistPort] = useState("");
  const isApprovedDynamicPort = (item: unknown): item is { protocol: "tcp" | "udp"; port: number } => {
    if (!item || typeof item !== "object") return false;
    const candidate = item as { protocol?: unknown; port?: unknown };
    return (candidate.protocol === "tcp" || candidate.protocol === "udp")
      && Number.isInteger(candidate.port)
      && Number(candidate.port) >= 32768
      && Number(candidate.port) <= 65535;
  };

  useEffect(() => { if (!collectorId && collectors[0]?.collector_id) setCollectorId(String(collectors[0].collector_id)); }, [collectorId, collectors]);
  const selectedCollector = useMemo(() => collectors.find((item) => item.collector_id === collectorId), [collectorId, collectors]);
  // Historical periods are operator-selected snapshots.  They must not change
  // just because the parent status poll receives a new collectors array.
  // Latest refreshes only when a successful inventory or traffic marker moves.
  const latestDataMarker = historical ? "historical" : `${selectedCollector?.last_successful_inventory_at ?? ""}|${selectedCollector?.last_successful_traffic_at ?? ""}`;
  useEffect(() => {
    if (!collectorId) return;
    let alive = true;
    setAllowlistMessage("");
    void api.waveGroupingPortAllowlist(collectorId).then((response) => {
      if (!alive) return;
      const ports = Array.isArray(response.ports) ? response.ports.filter(isApprovedDynamicPort) : [];
      setAllowlistPorts(ports);
    }).catch((reason: unknown) => {
      if (alive) setAllowlistMessage(reason instanceof Error ? reason.message : "Unable to load the wave-grouping policy.");
    });
    return () => { alive = false; };
  }, [collectorId]);

  async function saveAllowlist(ports: { protocol: "tcp" | "udp"; port: number }[]) {
    setAllowlistMessage("");
    try {
      const response = await api.updateWaveGroupingPortAllowlist(collectorId, ports);
      const saved = Array.isArray(response.ports) ? response.ports.filter(isApprovedDynamicPort) : [];
      setAllowlistPorts(saved);
      setAllowlistMessage("Saved. The automatic wave recommendation has been recalculated.");
      setManualRefresh((current) => current + 1);
    } catch (reason) {
      setAllowlistMessage(reason instanceof Error ? reason.message : "Unable to save the wave-grouping policy.");
    }
  }

  function addAllowlistPort() {
    const port = Number(newAllowlistPort);
    if (!Number.isInteger(port) || port < 32768 || port > 65535) {
      setAllowlistMessage("Enter a dynamic/private port between 32768 and 65535.");
      return;
    }
    if (allowlistPorts.some((item) => item.protocol === newAllowlistProtocol && item.port === port)) {
      setAllowlistMessage(`${newAllowlistProtocol.toUpperCase()}/${port} is already approved.`);
      return;
    }
    void saveAllowlist([...allowlistPorts, { protocol: newAllowlistProtocol, port }]);
    setNewAllowlistPort("");
  }

  function removeAllowlistPort(port: { protocol: "tcp" | "udp"; port: number }) {
    void saveAllowlist(
      allowlistPorts.filter((item) => item.protocol !== port.protocol || item.port !== port.port),
    );
  }
  useEffect(() => {
    if (!collectorId) return;
    const query = new URLSearchParams();
    let historicalAsOf: string | undefined;
    let label: string;
    if (historical) {
      let start: Date; let end: Date;
      if (range === "Custom time range") {
        if (!customStart || !customEnd) { setData(undefined); setError("Select both a start date and an end date to apply the custom range."); return; }
        if (customEnd < customStart) { setData(undefined); setError("The custom end date must be on or after the start date."); return; }
        start = new Date(`${customStart}T00:00:00.000Z`); end = new Date(`${customEnd}T23:59:59.999Z`);
        label = `${customStart} to ${customEnd} (${utc(start)} to ${utc(end)})`;
      } else {
        end = new Date(); start = new Date(end.getTime() - RANGES[range] * 86400000);
        label = `${range} (${utc(start)} to ${utc(end)})`;
      }
      query.set("historical", "true"); query.set("observed_after", start.toISOString()); query.set("observed_before", end.toISOString()); historicalAsOf = end.toISOString();
    } else {
      const lastTraffic = selectedCollector?.last_successful_traffic_at; const lastInventory = selectedCollector?.last_successful_inventory_at;
      if (typeof lastTraffic !== "string" || typeof lastInventory !== "string") { setData(undefined); setError("A successful inventory sync and traffic upload are required before the current inventory traffic window can be calculated."); return; }
      query.set("traffic_received_after", lastInventory); label = `Traffic received since the current inventory snapshot (${utc(new Date(lastInventory))} through ${utc(new Date(lastTraffic))})`;
    }
    let alive = true; setError("");
    void Promise.all([api.inventory(collectorId, historical, historicalAsOf), api.currentEnvironment(collectorId, query)]).then(([inventory, environment]) => {
      if (!alive) return;
      const { connections, waves, summary } = environment;
      setData({ inventory, connections, waves, summary, label, observedAfter: query.get("observed_after") ?? undefined, observedBefore: query.get("observed_before") ?? undefined, trafficReceivedAfter: query.get("traffic_received_after") ?? undefined });
      // Collector status refreshes every 15 seconds. Retain the operator's
      // current selection rather than returning to Wave 1 or the first VM.
      // Fall back only when the selected item no longer exists in refreshed data.
      setSelectedWave((current) => Math.min(current, Math.max(0, waves.length - 1)));
      setFocusVm((current) => inventory.some((item) => String(item.vm_uuid) === current)
        ? current
        : (inventory[0]?.vm_uuid ? String(inventory[0].vm_uuid) : ""));
    }).catch((reason: unknown) => { if (alive) setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed."); });
    return () => { alive = false; };
  }, [collectorId, customEnd, customStart, historical, latestDataMarker, manualRefresh, range]);

  const displayedVmIds = useMemo<Set<string>>(() => {
    if (!data) return new Set<string>();
    if (mapView === "Recommended wave") return new Set((data.waves[selectedWave]?.server_vm_uuids ?? []).map(String));
    if (!focusVm) return new Set<string>();
    const inventoryIds = new Set(data.inventory.map((item) => String(item.vm_uuid))); const result = new Set([focusVm]);
    for (const connection of data.connections) {
      if (String(connection.source_vm_uuid) !== focusVm && String(connection.destination_vm_uuid) !== focusVm) continue;
      for (const id of [connection.source_vm_uuid, connection.destination_vm_uuid]) if (id && inventoryIds.has(String(id))) result.add(String(id));
    }
    return result;
  }, [data, focusVm, mapView, selectedWave]);
  const graphInventory = useMemo(() => data?.inventory.filter((item) => displayedVmIds.has(String(item.vm_uuid))) ?? [], [data, displayedVmIds]);
  // Graphs use the same deduplicated, selected-scope evidence as the table.
  // The general /connections response is intentionally a newest-first bounded
  // sample for summary cards; using it here allowed busy internal Azure flows
  // to crowd external dependencies out of the graph.
  const approvedDynamicPorts = useMemo(
    () => new Set(allowlistPorts.map((item) => `${String(item.protocol).toLowerCase()}/${item.port}`)),
    [allowlistPorts],
  );
  const graphConnections = useMemo(() => (connectionPageData?.items ?? []).filter((item) => {
    const port = Number(item.destination_port);
    return port < 32768 || port > 65535 || approvedDynamicPorts.has(`${String(item.protocol).toLowerCase()}/${port}`);
  }).map((item) => ({
    ...item,
    approved_dynamic_port: approvedDynamicPorts.has(`${String(item.protocol).toLowerCase()}/${item.destination_port}`),
  })), [approvedDynamicPorts, connectionPageData]);
  const connectionVmIds = useMemo(
    () => mapView === "Focused VM" && focusVm ? [focusVm] : [...displayedVmIds],
    [displayedVmIds, focusVm, mapView],
  );
  useEffect(() => {
    if (!data || !collectorId) return;
    setConnectionPageData(undefined);
    const args = new URLSearchParams({ page: String(connectionPage), page_size: "250", deduplicate: "true", exclude_dynamic_private_ports: "false" });
    // A focused map retains direct peers as visual context.  Its table must
    // remain scoped to the selected VM's own observed connections only.
    for (const vmUuid of connectionVmIds) args.append("vm_uuid", vmUuid);
    if (data.observedAfter) args.set("observed_after", data.observedAfter);
    if (data.observedBefore) args.set("observed_before", data.observedBefore);
    if (data.trafficReceivedAfter) args.set("traffic_received_after", data.trafficReceivedAfter);
    let alive = true;
    void api.waveConnections(collectorId, args).then((response) => {
      if (alive) setConnectionPageData(response);
    }).catch((reason: unknown) => {
      if (alive) setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed.");
    });
    return () => { alive = false; };
  }, [collectorId, connectionPage, connectionVmIds, data]);
  const scopeLabel = mapView === "Recommended wave" && data?.waves[selectedWave] ? `Wave ${data.waves[selectedWave].wave} - ${(data.waves[selectedWave].server_names ?? []).length} VM(s)` : mapView === "Focused VM" ? (graphInventory.find((item) => String(item.vm_uuid) === focusVm)?.vm_name ?? "") : "";
  const observedIds = useMemo(() => new Set((data?.connections ?? []).flatMap((item) => [item.source_vm_uuid, item.destination_vm_uuid].filter(Boolean).map(String))), [data]);
  const inventoryRows = graphInventory.map((item) => ({ VM: item.vm_name ?? "", Hostname: item.hostname ?? "", "IP addresses": item.ips ?? "", "Power state": item.power_state ?? "", "Inventory status": inventoryStatus(item), Cluster: item.cluster ?? "", Folder: item.folder ?? "", "Operating system": item.os_name ?? "", "Traffic observed": observedIds.has(String(item.vm_uuid)) ? "Yes" : "No" }));
  const filteredInventoryRows = inventoryRows.filter((row) => !inventoryFilter || Object.values(row).join(" ").toLowerCase().includes(inventoryFilter.toLowerCase()));
  const scopedConnectionRows = (connectionPageData?.items ?? []).map((item) => ({ "Source VM": item.source_vm_name ?? "", Destination: item.destination_hostname ?? item.destination_vm_name ?? item.destination_ip ?? "", "Destination IP": item.destination_ip ?? "", "Connection type": item.destination_vm_uuid ? "Internal" : "External", Protocol: String(item.protocol ?? "").toUpperCase(), Port: item.destination_port ?? "", "Traffic classification": item.traffic_class ?? "", Evidence: item.evidence ?? "", Observations: item.observation_count ?? "" }));
  const filteredConnectionRows = scopedConnectionRows.filter((row) => !connectionFilter || Object.values(row).join(" ").toLowerCase().includes(connectionFilter.toLowerCase()));
  const poweredOn = (data?.inventory ?? []).filter((item) => item.power_state === "poweredOn").length;
  const observedInventory = (data?.inventory ?? []).filter((item) => observedIds.has(String(item.vm_uuid))).length;

  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>Current Environment</h1><p className="subtle">See the current discovered VM estate and observed network traffic. Recommended waves below are automatic discovery output, not a migration plan.</p>
    {!collectors.length ? <p className="empty-state">No registered collectors are available yet.</p> : <><section className="filters"><label>Collector<select value={collectorId} onChange={(event) => setCollectorId(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} - {item.tenant_id} - {item.collector_id}</option>)}</select></label><fieldset aria-label="Environment view"><label className="inventory-window-option"><input type="radio" checked={!historical} onChange={() => setHistorical(false)} />Current Inventory <span className="info-icon" tabIndex={0} role="img" aria-label="Current Inventory uses traffic accepted from the latest successful inventory sync through the latest successful traffic upload." data-tooltip="Includes traffic accepted from the latest successful inventory sync through the latest successful traffic upload. This keeps traffic aligned with the current inventory snapshot.">i</span></label><label><input type="radio" checked={historical} onChange={() => setHistorical(true)} />Historical period</label></fieldset>{historical ? <label>Observed connection time range<select value={range} onChange={(event) => setRange(event.target.value)}>{[...Object.keys(RANGES), "Custom time range"].map((item) => <option key={item}>{item}</option>)}</select></label> : null}{historical && range === "Custom time range" ? <><label>Custom start date<input type="date" value={customStart} onChange={(event) => setCustomStart(event.target.value)} /></label><label>Custom end date<input type="date" value={customEnd} onChange={(event) => setCustomEnd(event.target.value)} /></label></> : null}{historical ? <button type="button" onClick={() => setManualRefresh((current) => current + 1)}>Refresh now</button> : null}</section>
      <details className="content-card"><summary>Automatic wave grouping policy</summary><p className="subtle">Dynamic/private TCP and UDP ports never merge waves by default. Add a known application listener only when you want that exact port to form automatic waves.</p><div className="setup-form"><label>Protocol<select value={newAllowlistProtocol} onChange={(event) => setNewAllowlistProtocol(event.target.value as "tcp" | "udp")}><option value="tcp">TCP</option><option value="udp">UDP</option></select></label><label>Dynamic/private destination port<input aria-label="Dynamic/private destination port" type="number" min="32768" max="65535" value={newAllowlistPort} onChange={(event) => setNewAllowlistPort(event.target.value)} placeholder="33946" /></label><button className="button button--primary" type="button" onClick={() => void addAllowlistPort()}>Add approved port</button></div>{allowlistPorts.length ? <div className="table-scroll"><table><thead><tr><th>Protocol</th><th>Port</th><th>Action</th></tr></thead><tbody>{allowlistPorts.map((item) => <tr key={String(item.protocol) + "/" + item.port}><td>{String(item.protocol).toUpperCase()}</td><td>{item.port}</td><td><button type="button" onClick={() => void removeAllowlistPort(item)}>Remove</button></td></tr>)}</tbody></table></div> : <p className="empty-state">No dynamic/private ports are approved for automatic wave grouping.</p>}{allowlistMessage ? <p className="refresh-status">{allowlistMessage}</p> : null}</details>{error ? <div className="error-banner" role="alert">{error}</div> : null}
      {data ? <><p className="refresh-status">Observed traffic window: {data.label}. {historical ? "Historical" : "Current Inventory"} waves use automatic discovery only. Graphs include at most 100 unique edges.</p><h2>VM estate</h2><section className="metric-grid five"><Card label={historical ? "Historical inventory VMs" : "Current inventory VMs"} value={data.inventory.length} /><Card label="Powered-on VMs" value={poweredOn} /><Card label="Powered-off VMs" value={data.inventory.filter((item) => item.power_state === "poweredOff").length} /><Card label="VMs with observed traffic" value={observedInventory} /><Card label="VMs without observed traffic" value={data.inventory.length - observedInventory} /></section><h2>Recommended initial migration waves</h2><p className="subtle">These groups are generated from the selected inventory and observed traffic window. They are not an approved migration plan.</p><section className="metric-grid"><Card label="Recommended waves" value={Number(data.summary.wave_count ?? data.waves.length)} /><Card label="Waves with VM-to-VM connections" value={Number(data.summary.waves_with_observed_connections ?? 0)} /><Card label="Waves without VM-to-VM connections" value={Number(data.summary.waves_without_observed_connections ?? 0)} /></section>
        <section className="content-card"><fieldset aria-label="Map view"><label><input type="radio" checked={mapView === "Recommended wave"} onChange={() => { setMapView("Recommended wave"); setInventoryPage(1); setConnectionPage(1); }} />Recommended wave</label><label><input type="radio" checked={mapView === "Focused VM"} onChange={() => { setMapView("Focused VM"); setInventoryPage(1); setConnectionPage(1); }} />Focused VM</label></fieldset><label className="graph-control">Graph zoom: {zoom}%<input type="range" min="70" max="200" step="10" value={zoom} onChange={(event) => setZoom(Number(event.target.value))} /></label>{mapView === "Recommended wave" ? data.waves.length ? <><label className="collector-select">Recommended wave<select value={selectedWave} onChange={(event) => { setSelectedWave(Number(event.target.value)); setInventoryPage(1); setConnectionPage(1); }}>{data.waves.map((item, index) => <option key={index} value={index}>Wave {item.wave} - {(item.server_names ?? []).length} VM(s)</option>)}</select></label><p className="subtle">{data.waves[selectedWave]?.reason ?? "Automatic recommendation."}</p><p className="subtle">Blue/grey nodes are discovered VMs. Gold is a collapsed group of external destinations; use the observed-connections table for individual IP details.</p><GraphvizGraph inventory={graphInventory} connections={graphConnections} zoom={zoom} /></> : <p className="empty-state">No automatic migration-wave recommendation is available for this inventory.</p> : data.inventory.length ? <><label className="collector-select">Focus VM<select value={focusVm} onChange={(event) => { setFocusVm(event.target.value); setInventoryPage(1); setConnectionPage(1); }}>{data.inventory.map((item) => <option key={item.vm_uuid} value={item.vm_uuid}>{item.vm_name}</option>)}</select></label><p className="subtle">The selected VM is blue. Related discovered VMs are grey; external destinations are collapsed into one gold node.</p><GraphvizGraph inventory={graphInventory} connections={graphConnections} focusVmUuid={focusVm} zoom={zoom} /></> : <p className="empty-state">No current inventory VMs are available to show in the focused map.</p>}</section>
        <h2>{scopeLabel ? `Current inventory — ${scopeLabel}` : "Current inventory"}</h2><input className="table-filter" placeholder="Filter inventory by VM name, IP, cluster, folder, or operating system" value={inventoryFilter} onChange={(event) => { setInventoryFilter(event.target.value); setInventoryPage(1); }} /><PageTable rows={filteredInventoryRows} page={inventoryPage} setPage={setInventoryPage} /><h2>Current observed connections</h2><p className="refresh-status">Showing {connectionPageData?.total ?? 0} unique connection(s) for the selected {historical ? "historical period" : "Current Inventory"}.</p><input className="table-filter" placeholder="Filter connections by source, destination, IP, protocol, or port" value={connectionFilter} onChange={(event) => { setConnectionFilter(event.target.value); setConnectionPage(1); }} /><PageTable rows={filteredConnectionRows} page={1} setPage={() => undefined} />{connectionPageData && Math.ceil(connectionPageData.total / connectionPageData.page_size) > 1 ? <div className="pager"><button disabled={connectionPageData.page <= 1} onClick={() => setConnectionPage(connectionPageData.page - 1)}>Previous</button><span>Page {connectionPageData.page} of {Math.ceil(connectionPageData.total / connectionPageData.page_size)}; showing up to {connectionPageData.page_size} rows per page.</span><button disabled={connectionPageData.page >= Math.ceil(connectionPageData.total / connectionPageData.page_size)} onClick={() => setConnectionPage(connectionPageData.page + 1)}>Next</button></div> : null}</> : null}
    </>}</main>;
}
