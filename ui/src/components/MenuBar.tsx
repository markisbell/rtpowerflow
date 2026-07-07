import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { ActiveGrid, EngineStatus, ExportStatus, MeasurementsResponse, MeterMode, MeterPreset, RecordingInfo, RecordingStatus, Scenario } from "../types";
import { gridDisplayName } from "../gridname";
import { EstimationPanel } from "./EstimationMenu";
import type { LiveView } from "../App";

/** Desktop-style menu bar (Variante A): Netz · Simulation · Ansicht ·
 *  Messungen · Schätzung · Hilfe. One menu open at a time; the transport
 *  controls stay in the Live view's bottom bar. */
export default function MenuBar({ tab, onTab, active, live, onLive, onMeasChanged, onScenarioLoaded }: {
  tab: "config" | "live";
  onTab: (t: "config" | "live") => void;
  active: ActiveGrid | null;
  live: LiveView;
  onLive: (patch: Partial<LiveView>) => void;
  onMeasChanged: () => void;
  onScenarioLoaded: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (id: string) => setOpen((o) => (o === id ? null : id));
  const close = () => setOpen(null);

  // lightweight global poll so an active recording / running bulk export stays
  // visible even with all menus closed (both endpoints are near-free)
  const [rec, setRec] = useState<RecordingStatus | null>(null);
  const [exp, setExp] = useState<ExportStatus | null>(null);
  useEffect(() => {
    const load = () => {
      api.recording().then(setRec).catch(() => {});
      api.exportStatus().then(setExp).catch(() => {});
    };
    load();
    const iv = setInterval(load, 3000);
    return () => clearInterval(iv);
  }, []);
  const expPct = exp?.active && exp.steps_total
    ? Math.round(100 * (exp.steps_done ?? 0) / exp.steps_total) : null;

  return (
    <nav className="mbar">
      {open && <div className="mbar-overlay" onClick={close} />}
      <Menu id="netz" label={t("mbar.netz")} open={open} onToggle={toggle}>
        <NetzMenu tab={tab} onTab={(x) => { onTab(x); close(); }} active={active} />
      </Menu>
      <Menu id="sim" label={t("mbar.sim")} open={open} onToggle={toggle}>
        <SimMenu isOpen={open === "sim"} onScenarioLoaded={() => { onScenarioLoaded(); close(); }} />
      </Menu>
      <Menu id="view" label={t("mbar.view")} open={open} onToggle={toggle}>
        <ViewMenu live={live} onLive={onLive} disabled={tab !== "live"} />
      </Menu>
      <Menu id="meas" label={t("mbar.meas")} open={open} onToggle={toggle}>
        <MeasMenu isOpen={open === "meas"} onChanged={onMeasChanged} />
      </Menu>
      <Menu id="est" label={t("mbar.est")} open={open} onToggle={toggle}>
        <EstimationPanel />
      </Menu>
      <Menu id="help" label={t("mbar.help")} open={open} onToggle={toggle}>
        <a className="mi" href="/api/manual" target="_blank" rel="noreferrer">
          📖 {t("mbar.manual")}
        </a>
        <a className="mi" href="https://github.com/markisbell/EchtzeitNetzSimulator"
           target="_blank" rel="noreferrer">
          {t("mbar.source")}
        </a>
      </Menu>
      {(rec?.active || exp?.active) && (
        <div style={{ marginLeft: 8, display: "flex", gap: 12, alignItems: "center",
                      fontSize: "0.78rem", fontVariantNumeric: "tabular-nums" }}>
          {rec?.active && (
            <span style={{ color: "#f85149" }} title={t("rec.record")}>
              ⏺ {t("rec.recChip", { n: rec.steps })}
            </span>
          )}
          {exp?.active && (
            <span className="muted" title={t("rec.exportRunning")}>
              ⬇ {t("rec.expChip", { pct: expPct ?? 0 })}
            </span>
          )}
        </div>
      )}
    </nav>
  );
}

function Menu({ id, label, open, onToggle, children }: {
  id: string; label: string; open: string | null;
  onToggle: (id: string) => void; children: ReactNode;
}) {
  return (
    <div className="mbar-menu">
      <button className={open === id ? "on" : ""} onClick={() => onToggle(id)}>
        {label}
      </button>
      {open === id && <div className="mbar-drop">{children}</div>}
    </div>
  );
}

// ---- Netz ----------------------------------------------------------------
function NetzMenu({ tab, onTab, active }: {
  tab: string; onTab: (t: "config" | "live") => void;
  active: ActiveGrid | null;
}) {
  const { t } = useTranslation();
  return (
    <>
      <button className={"mi" + (tab === "config" ? " on" : "")} onClick={() => onTab("config")}>
        {t("mbar.netzStudio")}
      </button>
      <button className={"mi" + (tab === "live" ? " on" : "")} onClick={() => onTab("live")}>
        {t("mbar.toLive")}
      </button>
      <div className="mi-sep" />
      <div className="mi-hdr">{t("mbar.activeHdr")}</div>
      <div className="mi info">
        <span className="dot" />
        {active?.grid_id || active?.name
          ? <>{gridDisplayName(active.grid_id, active.name, t)}
              <span className="muted"> · {active.n_bus} {t("app.busShort")}</span></>
          : t("app.noGrid")}
      </div>
    </>
  );
}

// ---- Simulation (Start/Pause + Szenarien) ---------------------------------
function SimMenu({ isOpen, onScenarioLoaded }: { isOpen: boolean; onScenarioLoaded: () => void }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [scens, setScens] = useState<Scenario[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refresh = () => {
    api.status().then(setStatus).catch(() => {});
    api.scenarios().then((r) => setScens(r.scenarios)).catch(() => setScens([]));
  };
  useEffect(() => { if (isOpen) { setNote(null); refresh(); } }, [isOpen]);

  const toggleRun = async () => {
    if (!status) return;
    setStatus(await (status.running ? api.pause() : api.start()));
  };
  const save = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      await api.saveScenario(name.trim(), "");
      setName("");
      setNote(t("mbar.saved"));
      refresh();
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  };
  const load = async (id: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await api.loadScenario(id);
      onScenarioLoaded();
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button className="mi" onClick={toggleRun}>
        {status?.running ? <>⏸ {t("mbar.pause")}</> : <>▶ {t("mbar.start")}</>}
      </button>
      <div className="mi-sep" />
      <div className="mi-hdr">{t("scen.heading")}</div>
      <div className="mi row">
        <input value={name} placeholder={t("scen.name")}
               onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && save()} />
        <button className="mini" disabled={!name.trim() || busy} onClick={save}>
          {t("scen.save")}
        </button>
      </div>
      {scens === null && <div className="mi info muted">…</div>}
      {scens !== null && scens.length === 0 && <div className="mi info muted">{t("scen.none")}</div>}
      {scens?.map((s) => (
        <div className="mi row" key={s.id}>
          <button className="mi grow" title={s.description || s.name} disabled={busy}
                  onClick={() => load(s.id)}>
            {s.name}
          </button>
          <button className="mini" title={t("scen.delete")} disabled={busy}
                  onClick={() => api.deleteScenario(s.id).then(refresh).catch(() => {})}>
            ✕
          </button>
        </div>
      ))}
      {note && <div className="mi info muted">{note}</div>}
      <div className="mi-sep" />
      <ExportBlock isOpen={isOpen} />
    </>
  );
}

// ---- Export & Aufzeichnung (Bulk-Tagesexport + Live-Rekorder) ---------------
function fmtBytes(b: number): string {
  return b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
}

function ExportBlock({ isOpen }: { isOpen: boolean }) {
  const { t } = useTranslation();
  const [rec, setRec] = useState<RecordingStatus | null>(null);
  const [exp, setExp] = useState<ExportStatus | null>(null);
  const [list, setList] = useState<RecordingInfo[] | null>(null);
  const [days, setDays] = useState(1);
  const [estimate, setEstimate] = useState(true);
  const [note, setNote] = useState<string | null>(null);

  // poll while the menu is open so progress + list stay live
  useEffect(() => {
    if (!isOpen) return;
    setNote(null);
    const load = () => {
      api.exportStatus().then(setExp).catch(() => {});
      api.recordings().then((r) => { setList(r.recordings); setRec(r.active); }).catch(() => {});
    };
    load();
    const iv = setInterval(load, 2000);
    return () => clearInterval(iv);
  }, [isOpen]);

  const act = (p: Promise<unknown>) =>
    p.then(() => setNote(null)).catch((e) => setNote(String(e))).finally(() => {
      api.exportStatus().then(setExp).catch(() => {});
      api.recordings().then((r) => { setList(r.recordings); setRec(r.active); }).catch(() => {});
    });

  const pct = exp?.active && exp.steps_total
    ? Math.round(100 * (exp.steps_done ?? 0) / exp.steps_total) : 0;

  return (
    <>
      <div className="mi-hdr">{t("rec.heading")}</div>
      {!exp?.active && (
        <div className="mi row">
          <input type="number" min={1} max={366} value={days} style={{ width: "3.6em" }}
                 onChange={(e) => setDays(Math.max(1, Math.min(366, Number(e.target.value) || 1)))} />
          <label className="muted" style={{ display: "flex", alignItems: "center", gap: 4,
                                            fontSize: "0.74rem", cursor: "pointer" }}
                 title={t("rec.estimateTitle")}>
            <input type="checkbox" checked={estimate}
                   onChange={(e) => setEstimate(e.target.checked)} />
            🧮
          </label>
          <button className="mini" onClick={() => act(api.exportDays(days, estimate))}>
            ⬇ {t("rec.exportDays")}
          </button>
        </div>
      )}
      {exp?.active && (
        <div className="mi row">
          <span className="muted grow" style={{ fontSize: "0.76rem", fontVariantNumeric: "tabular-nums" }}>
            ⬇ {t("rec.expChip", { pct })}{exp.eta_seconds != null && ` · ~${exp.eta_seconds} s`}
          </span>
          <button className="mini" onClick={() => act(api.exportCancel())}>{t("rec.cancel")}</button>
        </div>
      )}
      {exp?.error && <div className="mi info muted">{t("rec.error")}: {exp.error}</div>}
      <button className="mi" onClick={() => act(rec?.active ? api.recordingStop() : api.recordingStart())}>
        {rec?.active
          ? <>⏹ {t("rec.recordStop")} <span className="muted sub">{rec.steps} {t("rec.steps")}</span></>
          : <><span style={{ color: "#f85149" }}>⏺</span> {t("rec.record")}</>}
      </button>
      {list !== null && list.length === 0 && <div className="mi info muted">{t("rec.none")}</div>}
      {list?.map((r) => (
        <div className="mi row" key={r.id}>
          <a className="mi grow" href={api.recordingDownloadUrl(r.id)}
             title={`${r.grid ?? ""} · ${r.steps ?? "?"} ${t("rec.steps")} · ${fmtBytes(r.bytes)}`}>
            💾 {r.id}
          </a>
          <button className="mini" title={t("rec.delete")}
                  onClick={() => act(api.deleteRecording(r.id))}>
            ✕
          </button>
        </div>
      ))}
      {note && <div className="mi info muted">{note}</div>}
    </>
  );
}

// ---- Ansicht ---------------------------------------------------------------
function ViewMenu({ live, onLive, disabled }: {
  live: LiveView; onLive: (p: Partial<LiveView>) => void; disabled: boolean;
}) {
  const { t } = useTranslation();
  const radio = (on: boolean, label: string, click: () => void) => (
    <button className="mi" disabled={disabled} onClick={click}>
      <span className="chk">{on ? "●" : ""}</span>{label}
    </button>
  );
  return (
    <>
      {disabled && <div className="mi info muted">{t("mbar.viewOnlyLive")}</div>}
      <div className="mi-hdr">{t("mbar.displayHdr")}</div>
      {radio(live.layout === "map", `🗺 ${t("live.map")}`, () => onLive({ layout: "map" }))}
      {radio(live.layout === "tree", t("live.schematic"), () => onLive({ layout: "tree" }))}
      <button className="mi" disabled={disabled} onClick={() => onLive({ showValues: !live.showValues })}>
        <span className="chk">{live.showValues ? "✓" : ""}</span>{t("live.values")}
      </button>
      <div className="mi-sep" />
      <div className="mi-hdr">{t("mbar.sightHdr")}</div>
      {radio(live.viewMode === "truth", `👁 ${t("mbar.sightTruth")}`, () => onLive({ viewMode: "truth" }))}
      {radio(live.viewMode === "observed", t("mbar.sightObserved"), () => onLive({ viewMode: "observed" }))}
      {radio(live.viewMode === "est", `🧮 ${t("live.estimate")}`, () => onLive({ viewMode: "est" }))}
    </>
  );
}

// ---- Messungen --------------------------------------------------------------
function MeasMenu({ isOpen, onChanged }: { isOpen: boolean; onChanged: () => void }) {
  const { t } = useTranslation();
  const [placement, setPlacement] = useState<MeasurementsResponse | null>(null);

  const refresh = () => api.measurements().then(setPlacement).catch(() => {});
  useEffect(() => { if (isOpen) refresh(); }, [isOpen]);

  const preset = async (name: MeterPreset) => {
    setPlacement(await api.meterPreset(name));
    onChanged();
  };
  const mode = async (name: MeterMode) => {
    setPlacement(await api.meterMode(name));
    onChanged();
  };

  const cov = placement?.coverage;
  return (
    <>
      <button className="mi" onClick={() => preset("all_nodes")}>{t("meas.presetAllNodes")}</button>
      <button className="mi" onClick={() => preset("substation_trafos")}>{t("meas.presetSubstations")}</button>
      <button className="mi" onClick={() => preset("all_trafos")}>{t("meas.presetAllTrafos")}</button>
      <button className="mi" onClick={() => preset("clear")}>🗑 {t("meas.clear")}</button>
      <div className="mi-sep" />
      <button className="mi" onClick={() => mode("full")}>
        <span className="chk">{placement?.mode === "full" ? "●" : ""}</span>{t("meas.modeFull")}
        <span className="muted sub">{t("mbar.taf9Sub")}</span>
      </button>
      <button className="mi" onClick={() => mode("standard")}>
        <span className="chk">{placement?.mode === "standard" ? "●" : ""}</span>{t("meas.modeStd")}
        <span className="muted sub">{t("mbar.taf7Sub")}</span>
      </button>
      <div className="mi-sep" />
      <div className="mi info muted">
        {t("meas.coverage")}: {cov
          ? t("meas.coverageVal", { nodes: cov.n_node_meter, totalNodes: cov.n_bus,
                                    trafos: cov.n_trafo_meter, totalTrafos: cov.n_trafo })
          : "—"}
      </div>
    </>
  );
}
