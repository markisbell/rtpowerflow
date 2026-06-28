import { useEffect, useState } from "react";
import { api } from "./api";
import type { ActiveGrid } from "./types";
import GridBrowser from "./views/GridBrowser";
import LoadStudio from "./views/LoadStudio";
import LivePowerFlow from "./views/LivePowerFlow";

type Tab = "grids" | "loads" | "live";

export default function App() {
  const [tab, setTab] = useState<Tab>("grids");
  const [selectedGrid, setSelectedGrid] = useState<string | null>(null);
  const [active, setActive] = useState<ActiveGrid | null>(null);

  const refreshActive = () => api.active().then(setActive).catch(() => {});
  useEffect(() => {
    refreshActive();
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          netzsim <span className="muted">realtime power flow</span>
        </div>
        <nav className="tabs">
          <button className={tab === "grids" ? "on" : ""} onClick={() => setTab("grids")}>
            1 · Grid
          </button>
          <button
            className={tab === "loads" ? "on" : ""}
            onClick={() => setTab("loads")}
            disabled={!selectedGrid}
            title={selectedGrid ? "" : "Pick a grid first"}
          >
            2 · Loads
          </button>
          <button className={tab === "live" ? "on" : ""} onClick={() => setTab("live")}>
            3 · Live
          </button>
        </nav>
        <div className="active-chip">
          {active?.grid_id ? (
            <>
              <span className="dot" /> {active.name}
              <span className="muted">
                {" "}
                · {active.n_bus} bus · {active.load_source ?? "—"} loads
                {!!active.n_ev && ` · 🔌 ${active.n_ev}`}
                {!!active.n_pv && ` · ☀️ ${active.n_pv}`}
              </span>
            </>
          ) : (
            <span className="muted">{active?.name ?? "no grid"} (default)</span>
          )}
        </div>
      </header>

      <main className="content">
        {tab === "grids" && (
          <GridBrowser
            selected={selectedGrid}
            onSelect={setSelectedGrid}
            onContinue={() => setTab("loads")}
          />
        )}
        {tab === "loads" && selectedGrid && (
          <LoadStudio
            gridId={selectedGrid}
            onApplied={(a) => {
              setActive(a);
              setTab("live");
            }}
          />
        )}
        {tab === "live" && <LivePowerFlow onActive={refreshActive} />}
      </main>
    </div>
  );
}
