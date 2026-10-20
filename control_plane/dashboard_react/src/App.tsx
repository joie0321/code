import { useCallback, useEffect, useState } from "react";

import { api } from "./api";
import { CollectorStatusParity, EnrollCollectorParity, InventoryFilteredParity } from "./ControlPlanePages";
import { CurrentEnvironment } from "./CurrentEnvironment";
import { Reporting } from "./Reporting";

type Page = "environment" | "status" | "enroll" | "inventory" | "reports";

function App() {
  const [page, setPage] = useState<Page>("environment");
  const [collectors, setCollectors] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);

  const reloadCollectors = useCallback(async () => {
    try {
      const items = await api.collectors();
      setCollectors(items);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Cannot contact the Control Plane API.");
    }
  }, []);

  useEffect(() => {
    void reloadCollectors();
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") void reloadCollectors();
    }, 15_000);
    return () => window.clearInterval(interval);
  }, [reloadCollectors]);

  const titles: Record<Page, string> = {
    environment: "Current Environment",
    status: "Agent Collector's Status",
    enroll: "Enroll Agent Collector",
    inventory: "VM/Server Inventory",
    reports: "Migration Reports",
  };
  const navigationGroups: Array<{ label: string; pages: Page[] }> = [
    { label: "Agent Collectors", pages: ["enroll", "status"] },
    { label: "Discovery", pages: ["environment", "inventory"] },
    { label: "Reports", pages: ["reports"] },
  ];

  return <div className="app-shell">
    <aside className="sidebar" aria-label="Control Plane navigation">
      <div className="brand"><span className="brand__mark">M</span><span>Migration Discovery</span></div>
      <p className="sidebar__caption">Control Plane</p>
      <nav>{navigationGroups.map((group) => <section className="nav-group" key={group.label}><p className="nav-group__label">{group.label}</p>{group.pages.map((key) => <button className={`nav-item ${page === key ? "nav-item--active" : ""}`} key={key} type="button" onClick={() => setPage(key)}>{titles[key]}</button>)}</section>)}</nav>
    </aside>
    {error ? <main className="main-content"><div className="error-banner" role="alert">{error}</div></main> : null}
    {!error && page === "environment" ? <CurrentEnvironment collectors={collectors} /> : null}
    {!error && page === "status" ? <CollectorStatusParity collectors={collectors} onChanged={reloadCollectors} /> : null}
    {!error && page === "enroll" ? <EnrollCollectorParity collectors={collectors} /> : null}
    {!error && page === "inventory" ? <InventoryFilteredParity collectors={collectors} /> : null}
    {!error && page === "reports" ? <Reporting collectors={collectors} /> : null}
  </div>;
}

export default App;
