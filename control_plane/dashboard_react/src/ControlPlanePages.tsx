import { useEffect, useState, type FormEvent } from "react";

import { api } from "./api";
import { formatTimestamp } from "./time";

type Row = Record<string, any>;

function Card({ label, value }: { label: string; value: number }) {
  return <article className="metric-card"><i /><p>{label}</p><strong>{value}</strong></article>;
}

function SimpleTable({ rows }: { rows: Row[] }) {
  const fields = rows[0] ? Object.keys(rows[0]) : [];
  if (!fields.length) return <p className="empty-state">No data is available.</p>;
  return <div className="table-scroll"><table><thead><tr>{fields.map((field) => <th key={field}>{field}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{fields.map((field) => <td key={field}>{field.endsWith("_at") ? formatTimestamp(row[field]) : String(row[field] ?? "—")}</td>)}</tr>)}</tbody></table></div>;
}

export function CollectorStatus({ collectors, onChanged }: { collectors: Row[]; onChanged: () => void }) {
  const [selected, setSelected] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { if (!selected && collectors[0]?.collector_id) setSelected(String(collectors[0].collector_id)); }, [collectors, selected]);
  const online = collectors.filter((item) => item.status === "online").length;
  async function removeCollector() {
    try {
      const result = await api.remove(selected);
      setMessage(`Collector ${result.collector_id} and its retained data were deleted.`);
      setConfirmation(""); setSelected(""); onChanged();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed."); }
  }
  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>Agent Collector&apos;s Status</h1>
    <section className="metric-grid"><Card label="Registered agent collectors" value={collectors.length} /><Card label="Online agent collectors" value={online} /><Card label="Agent collectors needing attention" value={collectors.length - online} /></section>
    {message ? <div className="success-banner" role="status">{message}</div> : null}{error ? <div className="error-banner" role="alert">{error}</div> : null}<SimpleTable rows={collectors} />
    {collectors.length ? <section className="content-card dangerous"><h2>Delete collector agent and all retained data</h2><p className="subtle">This permanently deletes the selected collector, its VM inventory, observed connections, migration-wave data, and reconnection codes. This cannot be undone.</p><div className="setup-form"><label>Collector agent to delete<select value={selected} onChange={(event) => setSelected(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} · {item.tenant_id} · {item.collector_id}</option>)}</select></label><label>Type DELETE to confirm permanent removal<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label><button className="button button--danger" type="button" disabled={confirmation !== "DELETE" || !selected} onClick={() => void removeCollector()}>Permanently delete collector agent</button></div></section> : null}
  </main>;
}

export function CollectorStatusParity({ collectors, onChanged }: { collectors: Row[]; onChanged: () => void }) {
  const [selected, setSelected] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!selected && collectors[0]?.collector_id) setSelected(String(collectors[0].collector_id));
  }, [collectors, selected]);
  const online = collectors.filter((item) => item.status === "online").length;
  async function permanentlyDeleteCollector() {
    setMessage(""); setError("");
    try {
      const deleted = await api.remove(selected);
      setMessage(`Collector ${deleted.collector_id} and its retained data were deleted.`);
      setConfirmation(""); setSelected(""); onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed.");
    }
  }
  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>Agent Collector&apos;s Status</h1>
    <section className="metric-grid"><Card label="Registered agent collectors" value={collectors.length} /><Card label="Online agent collectors" value={online} /><Card label="Agent collectors needing attention" value={collectors.length - online} /></section>
    <SimpleTable rows={collectors} />
    {collectors.length ? <details className="content-card dangerous"><summary>Delete collector agent and all retained data</summary><div className="warning-banner">This permanently deletes the selected collector, its VM inventory, observed connections, migration-wave data, and reconnection codes. This cannot be undone.</div><div className="setup-form"><label>Collector agent to delete<select value={selected} onChange={(event) => setSelected(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} · {item.tenant_id} · {item.collector_id}</option>)}</select></label><label>Type DELETE to confirm permanent removal<input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label><button className="button button--danger" type="button" disabled={confirmation !== "DELETE" || !selected} onClick={() => void permanentlyDeleteCollector()}>Permanently delete collector agent</button></div></details> : null}
    {message ? <div className="success-banner" role="status">{message}</div> : null}{error ? <div className="error-banner" role="alert">{error}</div> : null}
  </main>;
}

export function EnrollCollectorParity({ collectors }: { collectors: Row[] }) {
  const [mode, setMode] = useState<"new" | "reconnect">("new");
  const [tenantId, setTenantId] = useState("");
  const [collectorId, setCollectorId] = useState("");
  const [expiry, setExpiry] = useState(15);
  const [result, setResult] = useState<{ code: string; expiresAt: string }>();
  const [error, setError] = useState("");
  useEffect(() => {
    if (!collectorId && collectors[0]?.collector_id) setCollectorId(String(collectors[0].collector_id));
  }, [collectorId, collectors]);
  function changeMode(next: "new" | "reconnect") {
    setMode(next); setResult(undefined); setError("");
  }
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setResult(undefined); setError("");
    try {
      const response = mode === "new" ? await api.enrollment(tenantId, expiry) : await api.reconnect(collectorId, expiry);
      setResult({ code: String(response.enrollment_code ?? response.reconnection_code ?? ""), expiresAt: String(response.expires_at ?? "") });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed.");
    }
  }
  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>Enroll an agent collector appliance</h1>
    <div className="warning-banner">Administrator-only operation. The enrollment code is displayed once; copy it directly into the customer-side collector setup page.</div>
    <fieldset className="mode-picker"><legend>Operation</legend><label><input type="radio" checked={mode === "new"} onChange={() => changeMode("new")} />New collector</label><label><input type="radio" checked={mode === "reconnect"} onChange={() => changeMode("reconnect")} />Reconnect existing collector</label></fieldset>
    {error ? <div className="error-banner" role="alert">{error}</div> : null}
    {mode === "reconnect" && !collectors.length ? <p className="empty-state">No existing collectors are available to reconnect.</p> : <form className="setup-form" onSubmit={(event) => void submit(event)}>{mode === "new" ? <label>Customer tenant ID<input value={tenantId} onChange={(event) => { setTenantId(event.target.value); setResult(undefined); }} /></label> : <label>Existing collector<select value={collectorId} onChange={(event) => { setCollectorId(event.target.value); setResult(undefined); }}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} · {item.tenant_id} · {item.collector_id}</option>)}</select></label>}<label>Code expiry<select value={expiry} onChange={(event) => { setExpiry(Number(event.target.value)); setResult(undefined); }}>{[15, 30, 60].map((item) => <option key={item} value={item}>{item}</option>)}</select></label><button className="button button--primary" type="submit">{mode === "new" ? "Generate one-time enrollment code" : "Generate one-time reconnection code"}</button></form>}
    {result ? <div className="success-banner" role="status">Code expires at {formatTimestamp(result.expiresAt)}.<code>{result.code}</code><span>Copy this code now. It cannot be retrieved again after this page reruns.</span></div> : null}
  </main>;
}

export function InventoryParity({ collectors }: { collectors: Row[] }) {
  const [collectorId, setCollectorId] = useState("");
  const [inventory, setInventory] = useState<Row[] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!collectorId && collectors[0]?.collector_id) setCollectorId(String(collectors[0].collector_id));
  }, [collectorId, collectors]);
  useEffect(() => {
    if (!collectorId) return;
    let alive = true; setInventory(null); setError("");
    void api.inventory(collectorId).then((items) => { if (alive) setInventory(items); }).catch((reason: unknown) => {
      if (alive) setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed.");
    });
    return () => { alive = false; };
  }, [collectorId]);
  const rows = (inventory ?? []).map((item) => ({ "VM name": item.vm_name ?? "", Hostname: item.hostname ?? "", "IP addresses": item.ips ?? "", "Power state": item.power_state ?? "", Cluster: item.cluster ?? "", Folder: item.folder ?? "", "Operating system": item.os_name ?? "" }));
  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>VM/Server Inventory</h1>
    {!collectors.length ? <p className="empty-state">No registered collectors are available yet.</p> : <><label className="collector-select">Collector<select value={collectorId} onChange={(event) => setCollectorId(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} · {item.tenant_id}</option>)}</select><small>Selected Collector ID: {collectorId}</small></label>{error ? <div className="error-banner" role="alert">{error}</div> : null}{inventory === null && !error ? <p className="empty-state">Loading inventory…</p> : null}{inventory !== null && !error ? <><section className="metric-grid"><Card label="Active discovered VMs" value={inventory.length} /><Card label="Powered-on VMs" value={inventory.filter((item) => item.power_state === "poweredOn").length} /></section>{rows.length ? <SimpleTable rows={rows} /> : <p className="empty-state">This collector has not uploaded an inventory snapshot yet.</p>}</> : null}</>}
  </main>;
}

export function InventoryFilteredParity({ collectors }: { collectors: Row[] }) {
  const [collectorId, setCollectorId] = useState("");
  const [inventory, setInventory] = useState<Row[] | null>(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    if (!collectorId && collectors[0]?.collector_id) setCollectorId(String(collectors[0].collector_id));
  }, [collectorId, collectors]);
  useEffect(() => {
    if (!collectorId) return;
    let alive = true; setInventory(null); setError(""); setFilter("");
    void api.inventory(collectorId).then((items) => { if (alive) setInventory(items); }).catch((reason: unknown) => {
      if (alive) setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed.");
    });
    return () => { alive = false; };
  }, [collectorId]);
  const rows = (inventory ?? []).map((item) => ({ "VM name": item.vm_name ?? "", Hostname: item.hostname ?? "", "IP addresses": item.ips ?? "", "Power state": item.power_state ?? "", Cluster: item.cluster ?? "", Folder: item.folder ?? "", "Operating system": item.os_name ?? "" }));
  const filteredRows = rows.filter((row) => !filter || Object.values(row).join(" ").toLowerCase().includes(filter.toLowerCase()));
  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>VM/Server Inventory</h1>
    {!collectors.length ? <p className="empty-state">No registered collectors are available yet.</p> : <><label className="collector-select">Collector<select value={collectorId} onChange={(event) => setCollectorId(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} · {item.tenant_id}</option>)}</select><small>Selected Collector ID: {collectorId}</small></label>{error ? <div className="error-banner" role="alert">{error}</div> : null}{inventory === null && !error ? <p className="empty-state">Loading inventory…</p> : null}{inventory !== null && !error ? <><section className="metric-grid"><Card label="Active discovered VMs" value={inventory.length} /><Card label="Powered-on VMs" value={inventory.filter((item) => item.power_state === "poweredOn").length} /></section>{rows.length ? <><input className="table-filter" placeholder="Filter inventory by VM name, hostname, IP, power state, cluster, folder, or operating system" value={filter} onChange={(event) => setFilter(event.target.value)} />{filteredRows.length ? <SimpleTable rows={filteredRows} /> : <p className="empty-state">No inventory VMs match the filter.</p>}</> : <p className="empty-state">This collector has not uploaded an inventory snapshot yet.</p>}</> : null}</>}
  </main>;
}

export function EnrollCollector({ collectors }: { collectors: Row[] }) {
  const [mode, setMode] = useState<"new" | "reconnect">("new");
  const [tenant, setTenant] = useState("");
  const [existing, setExisting] = useState("");
  const [expiry, setExpiry] = useState(15);
  const [code, setCode] = useState<{ value: string; expiresAt: string }>();
  const [error, setError] = useState("");
  useEffect(() => { if (!existing && collectors[0]?.collector_id) setExisting(String(collectors[0].collector_id)); }, [collectors, existing]);
  async function generate() {
    setCode(undefined); setError("");
    try {
      const result = mode === "new" ? await api.enrollment(tenant, expiry) : await api.reconnect(existing, expiry);
      setCode({ value: String(result.enrollment_code ?? result.reconnection_code ?? ""), expiresAt: String(result.expires_at ?? "") });
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed."); }
  }
  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>Enroll an agent collector appliance</h1><div className="error-banner">Administrator-only operation. The enrollment code is displayed once; copy it directly into the customer-side collector setup page.</div><fieldset className="mode-picker"><label><input type="radio" checked={mode === "new"} onChange={() => setMode("new")} />New collector</label><label><input type="radio" checked={mode === "reconnect"} onChange={() => setMode("reconnect")} />Reconnect existing collector</label></fieldset>{error ? <div className="error-banner">{error}</div> : null}
    {mode === "new" ? <section className="setup-form"><label>Customer tenant ID<input value={tenant} onChange={(event) => setTenant(event.target.value)} /></label><label>Code expiry<select value={expiry} onChange={(event) => setExpiry(Number(event.target.value))}>{[15, 30, 60].map((item) => <option key={item} value={item}>{item}</option>)}</select></label><button className="button button--primary" type="button" onClick={() => void generate()}>Generate one-time enrollment code</button></section> : collectors.length ? <section className="setup-form"><label>Existing collector<select value={existing} onChange={(event) => setExisting(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} · {item.tenant_id} · {item.collector_id}</option>)}</select></label><label>Code expiry<select value={expiry} onChange={(event) => setExpiry(Number(event.target.value))}>{[15, 30, 60].map((item) => <option key={item} value={item}>{item}</option>)}</select></label><button className="button button--primary" type="button" onClick={() => void generate()}>Generate one-time reconnection code</button></section> : <p className="empty-state">No existing collectors are available to reconnect.</p>}
    {code ? <div className="success-banner">Code expires at {formatTimestamp(code.expiresAt)}.<code>{code.value}</code><span>Copy this code now. It cannot be retrieved again after this page reruns.</span></div> : null}
  </main>;
}

export function Inventory({ collectors }: { collectors: Row[] }) {
  const [collectorId, setCollectorId] = useState("");
  const [inventory, setInventory] = useState<Row[]>([]);
  const [error, setError] = useState("");
  useEffect(() => { if (!collectorId && collectors[0]?.collector_id) setCollectorId(String(collectors[0].collector_id)); }, [collectorId, collectors]);
  useEffect(() => { if (!collectorId) return; let alive = true; setError(""); void api.inventory(collectorId).then((items) => { if (alive) setInventory(items); }).catch((reason) => { if (alive) setError(reason instanceof Error ? reason.message : "Control-plane dashboard request failed."); }); return () => { alive = false; }; }, [collectorId]);
  const rows = inventory.map((item) => ({ "VM name": item.vm_name ?? "", Hostname: item.hostname ?? "", "IP addresses": item.ips ?? "", "Power state": item.power_state ?? "", Cluster: item.cluster ?? "", Folder: item.folder ?? "", "Operating system": item.os_name ?? "" }));
  return <main className="main-content"><p className="eyebrow">React Control Plane</p><h1>VM/Server Inventory</h1>{!collectors.length ? <p className="empty-state">No registered collectors are available yet.</p> : <><label className="collector-select">Collector<select value={collectorId} onChange={(event) => setCollectorId(event.target.value)}>{collectors.map((item) => <option key={item.collector_id} value={item.collector_id}>{item.display_name} · {item.tenant_id}</option>)}</select><small>Selected Collector ID: {collectorId}</small></label>{error ? <div className="error-banner">{error}</div> : null}<section className="metric-grid"><Card label="Active discovered VMs" value={inventory.length} /><Card label="Powered-on VMs" value={inventory.filter((item) => item.power_state === "poweredOn").length} /></section>{rows.length ? <SimpleTable rows={rows} /> : <p className="empty-state">This collector has not uploaded an inventory snapshot yet.</p>}</>}</main>;
}
