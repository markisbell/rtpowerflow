import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "./api";
import type { ActiveGrid } from "./types";
import { gridDisplayName } from "./gridname";
import EstimationMenu from "./components/EstimationMenu";
import GridBrowser from "./views/GridBrowser";
import LoadStudio from "./views/LoadStudio";
import LivePowerFlow from "./views/LivePowerFlow";

type Tab = "grids" | "loads" | "live";

export default function App() {
  const { t, i18n } = useTranslation();
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
        <nav className="tabs">
          <button className={tab === "grids" ? "on" : ""} onClick={() => setTab("grids")}>
            {t("nav.grid")}
          </button>
          <button
            className={tab === "loads" ? "on" : ""}
            onClick={() => setTab("loads")}
            disabled={!selectedGrid}
          >
            {t("nav.loads")}
          </button>
          <button className={tab === "live" ? "on" : ""} onClick={() => setTab("live")}>
            {t("nav.live")}
          </button>
          <EstimationMenu />
        </nav>
        <div className="active-chip">
          {active?.grid_id ? (
            <>
              <span className="dot" /> {gridDisplayName(active.grid_id, active.name, t)}
              <span className="muted">
                {" "}
                · {active.n_bus} {t("app.busShort")} · {active.load_source ?? "—"} {t("app.loadsShort")}
                {!!active.n_ev && ` · 🔌 ${active.n_ev}`}
                {!!active.n_pv && ` · ☀️ ${active.n_pv}`}
              </span>
            </>
          ) : (
            <span className="muted">{t("app.defaultGrid", { name: active?.name ?? t("app.noGrid") })}</span>
          )}
          <div className="lang-switch">
            {(["de", "en"] as const).map((lng) => (
              <button key={lng} className={i18n.language === lng ? "on" : ""}
                      onClick={() => i18n.changeLanguage(lng)}>
                {lng.toUpperCase()}
              </button>
            ))}
          </div>
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
