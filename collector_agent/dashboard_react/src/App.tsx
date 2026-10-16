import { useCallback, useEffect, useMemo, useState } from "react";

import {
  getCollectorStatus,
  reconnectSource,
  registerSource,
  unregisterSource,
  type SourceType,
} from "./api/collector";
import type { CollectorSource, CollectorStatus, RejectedIpfixDatagram } from "./types";
import { PlatformSetup } from "./PlatformSetup";
import { formatTimestamp } from "./time";

const REFRESH_INTERVAL_MS = 15_000;
const CONTROL_PLANE_DEFAULT = "http://127.0.0.1:8100";

type DashboardPage = "status" | "setup" | SourceType;
type Operation = "register" | "reconnect" | "unregister" | null;

const SOURCE_TYPES: Array<{ value: SourceType; label: string; defaultName: string }> = [
  { value: "vmware", label: "VMware", defaultName: "vmware-vcenter-01" },
  { value: "azure", label: "Azure", defaultName: "azure-01" },
  { value: "aws", label: "AWS", defaultName: "aws-01" },
];

const NAVIGATION_GROUPS: Array<{ label: string; pages: Array<{ key: DashboardPage; label: string }> }> = [
  {
    label: "Collector Management",
    pages: [
      { key: "status", label: "Agent Status" },
      { key: "setup", label: "Collector Setup" },
    ],
  },
  {
    label: "Source Setup",
    pages: [
      { key: "vmware", label: "VMware Setup" },
      { key: "azure", label: "Azure Setup" },
      { key: "aws", label: "AWS Setup" },
    ],
  },
];

function displayValue(value: string | number | null | undefined): string {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function configured(value: boolean | undefined): string {
  return value ? "Configured" : "Not configured";
}

function sourceHealth(source: CollectorSource): { label: string; tone: string } {
  if (source.last_error) return { label: "Attention required", tone: "danger" };
  const ipfixStatus = source.ipfix_status?.trim().toLowerCase();
  const awsFlowLogStatus = source.aws_flow_log_status?.trim().toLowerCase();
  const azureFlowLogStatus = source.azure_flow_log_status?.trim().toLowerCase();
  const isCollecting = (source.source_type === "vmware"
    && (source.ipfix_exporter_count ?? 0) > 0
    && (ipfixStatus === "ok" || ipfixStatus === "running"))
    || awsFlowLogStatus === "ok" || awsFlowLogStatus === "running"
    || azureFlowLogStatus === "ok" || azureFlowLogStatus === "running";
  if (isCollecting) {
    return { label: "Collecting", tone: "success" };
  }
  return { label: "Waiting for setup", tone: "neutral" };
}

function MetricCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return (
    <article className="metric-card">
      <span className="metric-card__accent" aria-hidden="true" />
      <p className="metric-card__label">{label}</p>
      <p className="metric-card__value">{value}</p>
      <p className="metric-card__detail">{detail}</p>
    </article>
  );
}

function SourceTable({ sources }: { sources: CollectorSource[] }) {
  if (sources.length === 0) {
    return <p className="empty-state">No collection sources are registered on this appliance.</p>;
  }
  return (
    <div className="table-scroll" tabIndex={0} aria-label="Registered collection sources">
      <table>
        <thead>
          <tr>
            <th>Source</th>
            <th>Type</th>
            <th>Status</th>
            <th>Inventory</th>
            <th>Last inventory sync</th>
            <th>Last traffic sync</th>
            <th>Last heartbeat</th>
            <th>IPFIX</th>
            <th>Cloud Flow Logs</th>
            <th>Last error</th>
          </tr>
        </thead>
        <tbody>
          {sources.map((source) => {
            const health = sourceHealth(source);
            const flowLogState = source.source_type === "aws"
              ? source.aws_flow_log_status
              : source.source_type === "azure"
                ? source.azure_flow_log_status
                : undefined;
            return (
              <tr key={source.collector_id}>
                <td><strong>{displayValue(source.display_name)}</strong><span className="subtle-id">{source.collector_id}</span></td>
                <td>{displayValue(source.source_type)}</td>
                <td><span className={`status-badge status-badge--${health.tone}`}>{health.label}</span></td>
                <td>{displayValue(source.last_inventory_vm_count)} VM(s)</td>
                <td>{formatTimestamp(source.last_inventory_sync_at)}</td>
                <td>{formatTimestamp(source.last_traffic_sync_at)}</td>
                <td>{formatTimestamp(source.last_heartbeat_at)}</td>
                <td>{source.source_type === "vmware" ? `${displayValue(source.ipfix_status)} · ${displayValue(source.ipfix_exporter_count)} exporter(s)` : "Not applicable"}</td>
                <td>{source.source_type === "vmware" ? "Not applicable" : `${configured(source.source_type === "aws" ? source.aws_flow_logs_configured : source.azure_flow_logs_configured)}${flowLogState ? ` · ${flowLogState}` : ""}`}</td>
                <td>{displayValue(source.last_error)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RejectedDatagrams({ datagrams }: { datagrams: RejectedIpfixDatagram[] }) {
  if (datagrams.length === 0) return null;
  return (
    <section className="warning-panel" aria-labelledby="ipfix-warning-title">
      <div>
        <p className="eyebrow">IPFIX attention required</p>
        <h2 id="ipfix-warning-title">Unassigned IPFIX exporters are sending data to this appliance</h2>
        <p>Assign each exporter IP to its correct VMware source before relying on its traffic observations.</p>
      </div>
      <div className="table-scroll" tabIndex={0} aria-label="Unassigned IPFIX datagrams">
        <table>
          <thead><tr><th>Exporter IP</th><th>Rejected datagrams</th><th>Last seen</th><th>Reason</th></tr></thead>
          <tbody>
            {datagrams.map((datagram) => (
              <tr key={`${datagram.exporter_ip}-${datagram.reason ?? "unknown"}`}>
                <td>{datagram.exporter_ip}</td>
                <td>{datagram.datagrams_rejected}</td>
                <td>{formatTimestamp(datagram.last_rejected_at)}</td>
                <td>{displayValue(datagram.reason)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function SourceSetupPage({
  sources,
  onComplete,
}: {
  sources: CollectorSource[];
  onComplete: (message: string) => Promise<void>;
}) {
  const [operation, setOperation] = useState<Operation>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [registration, setRegistration] = useState({
    controlPlaneUrl: CONTROL_PLANE_DEFAULT,
    tenantId: "lab-customer",
    enrollmentCode: "",
    displayName: "vmware-vcenter-01",
    sourceType: "vmware" as SourceType,
  });
  const [reconnection, setReconnection] = useState({
    controlPlaneUrl: CONTROL_PLANE_DEFAULT,
    collectorId: "",
    reconnectionCode: "",
    displayName: "",
    sourceType: "vmware" as SourceType,
  });
  const [selectedCollectorId, setSelectedCollectorId] = useState("");
  const [unregisterConfirmed, setUnregisterConfirmed] = useState(false);

  const submitRegistration = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setMessage(null);
    setError(null);
    setOperation("register");
    try {
      const result = await registerSource({
        control_plane_url: registration.controlPlaneUrl.trim(),
        tenant_id: registration.tenantId.trim(),
        enrollment_code: registration.enrollmentCode,
        display_name: registration.displayName.trim(),
        source_type: registration.sourceType,
      });
      setMessage(`Source registered: ${result.collector_id}`);
      await onComplete(`Source registered: ${result.collector_id}`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to register source.");
    } finally {
      setRegistration((current) => ({ ...current, enrollmentCode: "" }));
      setOperation(null);
    }
  };

  const submitReconnection = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setMessage(null);
    setError(null);
    setOperation("reconnect");
    try {
      const result = await reconnectSource({
        control_plane_url: reconnection.controlPlaneUrl.trim(),
        collector_id: reconnection.collectorId.trim(),
        reconnection_code: reconnection.reconnectionCode,
        display_name: reconnection.displayName.trim() || undefined,
        source_type: reconnection.sourceType,
      });
      setMessage(`Source reconnected: ${result.collector_id}`);
      await onComplete(`Source reconnected: ${result.collector_id}`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to reconnect source.");
    } finally {
      setReconnection((current) => ({ ...current, reconnectionCode: "" }));
      setOperation(null);
    }
  };

  const submitUnregister = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedCollectorId || !unregisterConfirmed) return;
    setMessage(null);
    setError(null);
    setOperation("unregister");
    try {
      const result = await unregisterSource(selectedCollectorId);
      setSelectedCollectorId("");
      setUnregisterConfirmed(false);
      setMessage(`Source removed from this appliance: ${result.collector_id}`);
      await onComplete(`Source removed from this appliance: ${result.collector_id}`);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to unregister source.");
    } finally {
      setOperation(null);
    }
  };

  return (
    <main className="main-content">
      <header className="page-header">
        <div>
          <p className="eyebrow">React dashboard · Phase 2</p>
          <h1>Collector Setup</h1>
          <p>Register, reconnect, or remove collection sources from this local appliance.</p>
        </div>
      </header>

      <section className="info-panel">
        <strong>Local-only configuration.</strong> Enrollment and reconnection codes are sent only to the local Collector API, are not saved in this browser, and are cleared after submission.
      </section>
      {message && <div className="success-banner" role="status">{message}</div>}
      {error && <div className="error-banner" role="alert">{error}</div>}

      <div className="setup-grid">
        <section className="content-card" aria-labelledby="register-source-title">
          <div className="section-heading"><div><p className="eyebrow">New source</p><h2 id="register-source-title">Register collection source</h2></div></div>
          <p className="form-intro">Create the one-time enrollment code from the OCI Control Plane before registering this source.</p>
          <form className="setup-form" onSubmit={submitRegistration}>
            <label>OCI Control Plane URL
              <input required type="url" value={registration.controlPlaneUrl} onChange={(event) => setRegistration({ ...registration, controlPlaneUrl: event.target.value })} autoComplete="off" />
            </label>
            <label>Customer tenant ID
              <input required maxLength={64} pattern="[A-Za-z0-9_-]+" value={registration.tenantId} onChange={(event) => setRegistration({ ...registration, tenantId: event.target.value })} autoComplete="off" />
            </label>
            <label>One-time enrollment code
              <input required type="password" minLength={24} maxLength={256} value={registration.enrollmentCode} onChange={(event) => setRegistration({ ...registration, enrollmentCode: event.target.value })} autoComplete="off" />
            </label>
            <label>Source type
              <select value={registration.sourceType} onChange={(event) => {
                const sourceType = event.target.value as SourceType;
                const selected = SOURCE_TYPES.find((item) => item.value === sourceType);
                setRegistration({ ...registration, sourceType, displayName: selected?.defaultName ?? registration.displayName });
              }}>
                {SOURCE_TYPES.map((source) => <option key={source.value} value={source.value}>{source.label}</option>)}
              </select>
            </label>
            <label>Source name
              <input required maxLength={128} pattern="[A-Za-z0-9 ._-]+" value={registration.displayName} onChange={(event) => setRegistration({ ...registration, displayName: event.target.value })} autoComplete="off" />
            </label>
            <button className="button button--primary" type="submit" disabled={operation !== null}>{operation === "register" ? "Registering…" : "Register source"}</button>
          </form>
        </section>

        <section className="content-card" aria-labelledby="reconnect-source-title">
          <div className="section-heading"><div><p className="eyebrow">Existing source</p><h2 id="reconnect-source-title">Reconnect after appliance restart</h2></div></div>
          <p className="form-intro">Reconnection rotates the local agent token while preserving the source history in the Control Plane.</p>
          <form className="setup-form" onSubmit={submitReconnection}>
            <label>OCI Control Plane URL
              <input required type="url" value={reconnection.controlPlaneUrl} onChange={(event) => setReconnection({ ...reconnection, controlPlaneUrl: event.target.value })} autoComplete="off" />
            </label>
            <label>Existing Collector ID
              <input required minLength={36} maxLength={36} value={reconnection.collectorId} onChange={(event) => setReconnection({ ...reconnection, collectorId: event.target.value })} autoComplete="off" />
            </label>
            <label>One-time reconnection code
              <input required type="password" minLength={24} maxLength={256} value={reconnection.reconnectionCode} onChange={(event) => setReconnection({ ...reconnection, reconnectionCode: event.target.value })} autoComplete="off" />
            </label>
            <label>Source type
              <select value={reconnection.sourceType} onChange={(event) => setReconnection({ ...reconnection, sourceType: event.target.value as SourceType })}>
                {SOURCE_TYPES.map((source) => <option key={source.value} value={source.value}>{source.label}</option>)}
              </select>
            </label>
            <label>Source name <span className="optional">(optional)</span>
              <input maxLength={128} pattern="[A-Za-z0-9 ._-]+" value={reconnection.displayName} onChange={(event) => setReconnection({ ...reconnection, displayName: event.target.value })} autoComplete="off" />
            </label>
            <button className="button button--secondary" type="submit" disabled={operation !== null}>{operation === "reconnect" ? "Reconnecting…" : "Reconnect source"}</button>
          </form>
        </section>
      </div>

      <section className="content-card dangerous-action" aria-labelledby="unregister-source-title">
        <div className="section-heading"><div><p className="eyebrow">Local removal</p><h2 id="unregister-source-title">Unregister source from this appliance</h2></div></div>
        <p className="form-intro">This stops local collection and removes local in-memory credentials. Control Plane inventory, traffic, plan, and report history are retained.</p>
        {sources.length === 0 ? <p className="empty-state">No registered source is available to unregister.</p> : (
          <form className="setup-form setup-form--inline" onSubmit={submitUnregister}>
            <label>Registered source
              <select required value={selectedCollectorId} onChange={(event) => { setSelectedCollectorId(event.target.value); setUnregisterConfirmed(false); }}>
                <option value="">Select a source</option>
                {sources.map((source) => <option key={source.collector_id} value={source.collector_id}>{source.display_name} — {source.collector_id}</option>)}
              </select>
            </label>
            <label className="confirmation"><input type="checkbox" checked={unregisterConfirmed} onChange={(event) => setUnregisterConfirmed(event.target.checked)} /> I understand the local collection and local credentials will be removed.</label>
            <button className="button button--danger" type="submit" disabled={operation !== null || !selectedCollectorId || !unregisterConfirmed}>{operation === "unregister" ? "Removing…" : "Unregister selected source"}</button>
          </form>
        )}
      </section>
    </main>
  );
}

export default function App() {
  const [page, setPage] = useState<DashboardPage>("status");
  const [status, setStatus] = useState<CollectorStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const latestStatus = await getCollectorStatus(signal);
      setStatus(latestStatus);
      setError(null);
      setLastRefresh(new Date());
    } catch (requestError) {
      if (requestError instanceof DOMException && requestError.name === "AbortError") return;
      setError(requestError instanceof Error ? requestError.message : "Unable to refresh collector status.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, REFRESH_INTERVAL_MS);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [refresh]);

  const sources = status?.sources ?? [];
  const datagrams = status?.unassigned_ipfix_datagrams ?? [];
  const exporterCount = useMemo(
    () => sources.reduce((total, source) => total + (source.ipfix_exporter_count ?? 0), 0),
    [sources],
  );
  const attentionCount = useMemo(
    () => sources.filter((source) => Boolean(source.last_error)).length,
    [sources],
  );
  const completeSetup = useCallback(async () => {
    await refresh();
  }, [refresh]);
  const collectorUnavailable = status === null && error !== null;

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="Collector dashboard navigation">
        <div className="brand"><span className="brand__mark">M</span><span>Migration Discovery</span></div>
        <p className="sidebar__caption">Collector Appliance</p>
        <nav>
          {NAVIGATION_GROUPS.map((group) => (
            <section className="nav-group" key={group.label} aria-label={group.label}>
              <p className="nav-group__label">{group.label}</p>
              {group.pages.map((item) => (
                <button className={`nav-item ${page === item.key ? "nav-item--active" : ""}`} key={item.key} type="button" onClick={() => setPage(item.key)} aria-current={page === item.key ? "page" : undefined}>{item.label}</button>
              ))}
            </section>
          ))}
        </nav>
        <p className="sidebar__security">Local setup only. Customer credentials remain on this appliance.</p>
      </aside>

      {collectorUnavailable ? <main className="main-content"><header className="page-header"><div><p className="eyebrow">Collector Appliance</p><h1>Migration Discovery Collector</h1></div></header><div className="error-banner" role="alert"><strong>Collector unavailable.</strong> {error} Start the local Collector API, then refresh this page.</div></main> : page === "setup" ? <SourceSetupPage sources={sources} onComplete={completeSetup} /> : page === "vmware" || page === "azure" || page === "aws" ? <PlatformSetup sourceType={page} sources={sources} onComplete={completeSetup} /> : <main id="agent-status" className="main-content">
        <header className="page-header">
          <div>
            <p className="eyebrow">React dashboard · Phase 1</p>
            <h1>Migration Discovery Collector</h1>
            <p>Monitor local collection health, source registration, and IPFIX routing.</p>
          </div>
          <button className="button button--primary" type="button" onClick={() => void refresh()} disabled={loading}>
            {loading ? "Refreshing…" : "↻ Refresh now"}
          </button>
        </header>

        <p className="refresh-status" aria-live="polite">
          Status refreshes every 15 seconds{lastRefresh ? ` · Last refreshed ${formatTimestamp(lastRefresh.toISOString())}` : ""}.
        </p>

        {error && <div className="error-banner" role="alert"><strong>Collector unavailable.</strong> {error} Start the local Collector API, then refresh.</div>}

        <section className="metric-grid" aria-label="Collector summary">
          <MetricCard label="Registered sources" value={String(sources.length)} detail="Sources configured on this appliance" />
          <MetricCard label="IPFIX exporters" value={String(exporterCount)} detail="Exporters assigned to VMware sources" />
          <MetricCard label="Sources needing attention" value={String(attentionCount)} detail="Sources with a latest local error" />
        </section>

        <section className="content-card" aria-labelledby="registered-sources-title">
          <div className="section-heading">
            <div><p className="eyebrow">Local Collector</p><h2 id="registered-sources-title">Registered sources</h2></div>
            <span className="phase-badge">Status monitoring</span>
          </div>
          <SourceTable sources={sources} />
        </section>

        <RejectedDatagrams datagrams={datagrams} />
      </main>}
    </div>
  );
}
