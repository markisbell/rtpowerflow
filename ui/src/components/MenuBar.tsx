import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { ActiveGrid, ExportStatus, MeasurementsResponse, MeterMode, MeterPreset, RecordingInfo, RecordingStatus, Scenario } from "../types";
import { EstimationPanel } from "./EstimationMenu";
import type { LiveView } from "../App";

/** Desktop-style menu bar (Variante B): Datei · Ansicht · Messungen · Hilfe
 *  plus an ALWAYS-VISIBLE Sicht segment (Lastfluss / Gemessen / Schätzung) —
 *  the three views are the platform's core concept, so switching them must
 *  not require menu digging. Menus are pure command lists following the
 *  classic Datei taxonomy (everything that loads, saves or exports lives
 *  there); items ending in "…" open a small dialog instead of embedding
 *  input fields in the dropdown. Start/Pause lives only in the Live view's
 *  bottom transport bar. Activity chips (⏺ recording / ⬇ export) stay
 *  visible with all menus closed and jump into the Datei menu on click. */
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
  const [dialog, setDialog] = useState<"scenario" | "export" | "estimation" | null>(null);
  const toggle = (id: string) => setOpen((o) => (o === id ? null : id));
  const close = () => setOpen(null);
  void active;   // the active grid is shown by the header chip next to the bar

  // lightweight global poll so an active recording / running bulk export stays
  // visible even with all menus closed (both endpoints are near-free)
  const [rec, setRec] = useState<RecordingStatus | null>(null);
  const [exp, setExp] = useState<ExportStatus | null>(null);
  const poll = () => {
    api.recording().then(setRec).catch(() => {});
    api.exportStatus().then(setExp).catch(() => {});
  };
  useEffect(() => {
    poll();
    const iv = setInterval(poll, 3000);
    return () => clearInterval(iv);
  }, []);
  const expPct = exp?.active && exp.steps_total
    ? Math.round(100 * (exp.steps_done ?? 0) / exp.steps_total) : null;

  return (
    <nav className="mbar">
      {open && <div className="mbar-overlay" onClick={close} />}
      <Menu id="datei" label={t("mbar.datei")} open={open} onToggle={toggle}>
        <DateiMenu isOpen={open === "datei"} tab={tab}
                   onTab={(x) => { onTab(x); close(); }}
                   onScenarioLoaded={() => { onScenarioLoaded(); close(); }}
                   onDialog={(d) => { setDialog(d); close(); }}
                   onChanged={poll} />
      </Menu>
      <Menu id="view" label={t("mbar.view")} open={open} onToggle={toggle}>
        <ViewMenu live={live} onLive={onLive} disabled={tab !== "live"} />
      </Menu>
      <Menu id="meas" label={t("mbar.meas")} open={open} onToggle={toggle}>
        <MeasMenu isOpen={open === "meas"} onChanged={onMeasChanged}
                  onDialog={(d) => { setDialog(d); close(); }} />
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

      {/* the Sicht is a MODE that changes everything on screen — permanently
          visible as a segmented control, never hidden in a dropdown */}
      <div className="mbar-seg" role="group" aria-label={t("mbar.sightHdr")}>
        <button className={live.viewMode === "truth" ? "on" : ""} disabled={tab !== "live"}
                title={t("mbar.sightTruth")} onClick={() => onLive({ viewMode: "truth" })}>
          👁 {t("mbar.segTruth")}
        </button>
        <button className={live.viewMode === "observed" ? "on" : ""} disabled={tab !== "live"}
                title={t("mbar.sightObserved")} onClick={() => onLive({ viewMode: "observed" })}>
          {t("mbar.segObserved")}
        </button>
        <button className={live.viewMode === "est" ? "on" : ""} disabled={tab !== "live"}
                title={t("live.estimate")} onClick={() => onLive({ viewMode: "est" })}>
          🧮 {t("mbar.segEst")}
        </button>
      </div>

      {(rec?.active || exp?.active) && (
        <div style={{ marginLeft: 8, display: "flex", gap: 8, alignItems: "center" }}>
          {rec?.active && (
            <button className="mbar-chip rec" title={t("rec.record")}
                    onClick={() => setOpen("datei")}>
              ⏺ {t("rec.recChip", { n: rec.steps })}
            </button>
          )}
          {exp?.active && (
            <button className="mbar-chip" title={t("rec.exportRunning")}
                    onClick={() => setOpen("datei")}>
              ⬇ {t("rec.expChip", { pct: expPct ?? 0 })}
            </button>
          )}
        </div>
      )}

      {dialog === "scenario" && <ScenarioDialog onClose={() => setDialog(null)} />}
      {dialog === "export" && <ExportDialog onClose={() => { setDialog(null); poll(); }} />}
      {dialog === "estimation" && (
        <Dialog title={`🧮 ${t("mbar.est")}`} onClose={() => setDialog(null)}>
          <EstimationPanel />
        </Dialog>
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

/** A flyout submenu ("Szenario laden ▸"): opens on hover or click and keeps
 *  the parent dropdown a short command list instead of an inline wall. */
function SubMenu({ label, children }: { label: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mi-sub" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button className="mi" onClick={() => setOpen((o) => !o)}>
        <span className="grow-label">{label}</span>
        <span className="sub-arrow">▸</span>
      </button>
      {open && <div className="mbar-drop sub">{children}</div>}
    </div>
  );
}

// ---- Datei (Netz öffnen · Szenarien · Daten-Export) -------------------------
function fmtBytes(b: number): string {
  return b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
}

function DateiMenu({ isOpen, tab, onTab, onScenarioLoaded, onDialog, onChanged }: {
  isOpen: boolean; tab: string;
  onTab: (t: "config" | "live") => void;
  onScenarioLoaded: () => void;
  onDialog: (d: "scenario" | "export") => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [scens, setScens] = useState<Scenario[] | null>(null);
  const [rec, setRec] = useState<RecordingStatus | null>(null);
  const [exp, setExp] = useState<ExportStatus | null>(null);
  const [list, setList] = useState<RecordingInfo[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  // poll while open so export progress + pack list stay live
  useEffect(() => {
    if (!isOpen) return;
    setNote(null);
    const load = () => {
      api.scenarios().then((r) => setScens(r.scenarios)).catch(() => setScens([]));
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
      onChanged();
    });

  const loadScen = async (id: string) => {
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

  const pct = exp?.active && exp.steps_total
    ? Math.round(100 * (exp.steps_done ?? 0) / exp.steps_total) : 0;

  return (
    <>
      <button className={"mi" + (tab === "config" ? " on" : "")} onClick={() => onTab("config")}>
        {t("mbar.netzStudio")}
      </button>
      <button className={"mi" + (tab === "live" ? " on" : "")} onClick={() => onTab("live")}>
        {t("mbar.toLive")}
      </button>
      <div className="mi-sep" />
      <button className="mi" onClick={() => onDialog("scenario")}>💾 {t("scen.saveDots")}</button>
      <SubMenu label={t("scen.loadSub")}>
        {scens === null && <div className="mi info muted">…</div>}
        {scens !== null && scens.length === 0 && <div className="mi info muted">{t("scen.none")}</div>}
        {scens?.map((s) => (
          <div className="mi row" key={s.id}>
            <button className="mi grow" title={s.description || s.name} disabled={busy}
                    onClick={() => loadScen(s.id)}>
              {s.name}
            </button>
            <button className="mini" title={t("scen.delete")} disabled={busy}
                    onClick={() => act(api.deleteScenario(s.id))}>
              ✕
            </button>
          </div>
        ))}
      </SubMenu>
      <div className="mi-sep" />
      {!exp?.active && (
        <button className="mi" onClick={() => onDialog("export")}>⬇ {t("rec.exportDaysDots")}</button>
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
      <SubMenu label={t("rec.recordings")}>
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
      </SubMenu>
      {note && <div className="mi info muted">{note}</div>}
    </>
  );
}

// ---- Ansicht (nur noch Darstellung — die Sicht lebt im Segment) --------------
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
    </>
  );
}

// ---- Messungen (Presets + TAF-Modus + Schätz-Richtlinie-Dialog) --------------
function MeasMenu({ isOpen, onChanged, onDialog }: {
  isOpen: boolean; onChanged: () => void;
  onDialog: (d: "estimation") => void;
}) {
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
      <div className="mi info muted">{t("meas.perDeviceHint")}</div>
      <div className="mi-sep" />
      <button className="mi" onClick={() => onDialog("estimation")}>🧮 {t("mbar.estConfigDots")}</button>
      <div className="mi info muted">
        {t("meas.coverage")}: {cov
          ? t("meas.coverageVal", { nodes: cov.n_node_meter, totalNodes: cov.n_bus,
                                    trafos: cov.n_trafo_meter, totalTrafos: cov.n_trafo })
          : "—"}
      </div>
    </>
  );
}

// ---- Dialoge -----------------------------------------------------------------
function Dialog({ title, onClose, children }: {
  title: string; onClose: () => void; children: ReactNode;
}) {
  useEffect(() => {
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [onClose]);
  return (
    <>
      <div className="dlg-overlay" onClick={onClose} />
      <div className="dlg" role="dialog" aria-label={title}>
        <div className="dlg-head">
          <span>{title}</span>
          <button className="mini" onClick={onClose}>✕</button>
        </div>
        {children}
      </div>
    </>
  );
}

function ScenarioDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    if (!name.trim() || busy) return;
    setBusy(true);
    try {
      await api.saveScenario(name.trim(), "");
      onClose();
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  };
  return (
    <Dialog title={`💾 ${t("scen.saveTitle")}`} onClose={onClose}>
      <div className="dlg-row">
        <input autoFocus value={name} placeholder={t("scen.name")}
               onChange={(e) => setName(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && save()} />
      </div>
      <div className="dlg-note">{t("scen.saveHint")}</div>
      {err && <div className="dlg-note">{err}</div>}
      <div className="dlg-actions">
        <button className="mini" onClick={onClose}>{t("rec.cancel")}</button>
        <button className="mini primary" disabled={!name.trim() || busy} onClick={save}>
          {t("scen.save")}
        </button>
      </div>
    </Dialog>
  );
}

function ExportDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const [days, setDays] = useState(1);
  const [estimate, setEstimate] = useState(true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const start = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await api.exportDays(days, estimate);
      onClose();
    } catch (e) {
      setErr(String(e));
      setBusy(false);
    }
  };
  return (
    <Dialog title={`⬇ ${t("rec.exportTitle")}`} onClose={onClose}>
      <div className="dlg-row">
        <label>{t("rec.days")}</label>
        <input type="number" min={1} max={366} value={days} autoFocus style={{ width: "5em" }}
               onChange={(e) => setDays(Math.max(1, Math.min(366, Number(e.target.value) || 1)))}
               onKeyDown={(e) => e.key === "Enter" && start()} />
      </div>
      <label className="dlg-row" style={{ cursor: "pointer" }} title={t("rec.estimateTitle")}>
        <input type="checkbox" checked={estimate} onChange={(e) => setEstimate(e.target.checked)} />
        <span>{t("rec.estimate")}</span>
      </label>
      <div className="dlg-note">{t("rec.exportHint")}</div>
      {err && <div className="dlg-note">{err}</div>}
      <div className="dlg-actions">
        <button className="mini" onClick={onClose}>{t("rec.cancel")}</button>
        <button className="mini primary" disabled={busy} onClick={start}>
          {t("rec.exportStart")}
        </button>
      </div>
    </Dialog>
  );
}
