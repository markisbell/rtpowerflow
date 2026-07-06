import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "./api";
import type { ActiveGrid } from "./types";
import { gridDisplayName } from "./gridname";
import MenuBar from "./components/MenuBar";
import NetzStudio from "./views/NetzStudio";
import LivePowerFlow from "./views/LivePowerFlow";

type Tab = "config" | "live";

// The Live view's display settings, lifted here so the menu bar (Ansicht)
// and the view itself share one source of truth.
export interface LiveView {
  layout: "map" | "tree";
  showValues: boolean;
  viewMode: "truth" | "observed" | "est";
}

export default function App() {
  const { t, i18n } = useTranslation();
  const [tab, setTab] = useState<Tab>("live");   // a real program opens its main view
  const [selectedGrid, setSelectedGrid] = useState<string | null>(null);
  const [active, setActive] = useState<ActiveGrid | null>(null);
  const [live, setLive] = useState<LiveView>({ layout: "tree", showValues: false, viewMode: "truth" });
  const patchLive = (p: Partial<LiveView>) => setLive((v) => ({ ...v, ...p }));
  const [measStamp, setMeasStamp] = useState(0);   // menu meter actions -> Live refetch
  const [liveKey, setLiveKey] = useState(0);       // scenario load -> full Live remount

  const refreshActive = () => api.active().then(setActive).catch(() => {});
  useEffect(() => {
    refreshActive();
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <span className="brand">netzsim</span>
        <MenuBar tab={tab} onTab={setTab} active={active}
                 live={live} onLive={patchLive}
                 onMeasChanged={() => setMeasStamp((n) => n + 1)}
                 onScenarioLoaded={() => { refreshActive(); setTab("live"); setLiveKey((n) => n + 1); }} />
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
        {tab === "config" && (
          <NetzStudio
            selected={selectedGrid}
            onSelect={setSelectedGrid}
            onApplied={(a) => {
              setActive(a);
              setTab("live");
              setLiveKey((n) => n + 1);
            }}
          />
        )}
        {tab === "live" && (
          <LivePowerFlow key={liveKey} onActive={refreshActive}
                         view={live} onView={patchLive} measStamp={measStamp} />
        )}
      </main>
    </div>
  );
}
