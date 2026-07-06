import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type {
  ActiveGrid, Archetype, AssignResponse, GridListItem, GridPreview, LoadgenPolicy,
} from "../types";
import { fmt } from "../scales";
import Sparkline from "../components/Sparkline";
import StructureDiagram from "../components/StructureDiagram";
import { gridDisplayName } from "../gridname";

interface Props {
  selected: string | null;
  onSelect: (id: string | null) => void;
  onApplied: (active: ActiveGrid) => void;
}

/** The one grid workflow view: pick or import a grid, configure loads and
 *  generation, check the transformer loading, start the simulation. */
export default function NetzStudio({ selected, onSelect, onApplied }: Props) {
  const { t } = useTranslation();
  const [grids, setGrids] = useState<GridListItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [gridPrev, setGridPrev] = useState<GridPreview | null>(null);

  // ---- load configuration ----
  const [archetypes, setArchetypes] = useState<Archetype[] | null>(null);
  const [available, setAvailable] = useState(true);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<"round_robin" | "random">("round_robin");
  const [seed, setSeed] = useState(0);
  const [scale, setScale] = useState(1);
  const [pf, setPf] = useState(0.95);
  const [evPen, setEvPen] = useState(0);
  const [evKw, setEvKw] = useState(11);
  const [pvPen, setPvPen] = useState(0);
  const [pvKwp, setPvKwp] = useState(5);
  const [mfh, setMfh] = useState(true);            // 3-6 households per building
  const [preview, setPreview] = useState<AssignResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const refreshGrids = () =>
    api.grids().then((r) => setGrids(r.grids)).catch((e) => setError(String(e)));

  useEffect(() => {
    refreshGrids();
    api.archetypes().then((r) => {
      setAvailable(r.available);
      setArchetypes(r.archetypes);
      setChosen(new Set(r.archetypes.map((a) => a.id)));
    });
  }, []);

  const library = useMemo(
    () => (grids ?? []).filter((g) => g.character !== "user")
      .sort((a, b) => (a.nodes ?? 0) - (b.nodes ?? 0)),
    [grids]);
  const own = useMemo(
    () => (grids ?? []).filter((g) => g.character === "user")
      .sort((a, b) => a.name.localeCompare(b.name)),
    [grids]);

  const policy = useMemo<LoadgenPolicy>(() => ({
    archetypes: chosen.size ? [...chosen] : null,
    mode, seed, scale,
    power_factor: pf,
    ev_penetration: evPen,
    ev_charger_kw: evKw,
    pv_penetration: pvPen,
    pv_kwp: pvKwp,
    mfh: mfh ? "auto" : "off",
  }), [chosen, mode, seed, scale, pf, evPen, evKw, pvPen, pvKwp, mfh]);

  // topology preview of the selected grid (stale responses ignored)
  const reqRef = useRef(0);
  useEffect(() => {
    setGridPrev(null);
    setPreview(null);
    if (!selected) return;
    const my = ++reqRef.current;
    api.gridPreview(selected)
      .then((p) => { if (reqRef.current === my) setGridPrev(p); })
      .catch((e) => { if (reqRef.current === my) setError(String(e)); });
  }, [selected]);

  // auto-preview the assignment (debounced) whenever grid or policy changes
  const asgRef = useRef(0);
  useEffect(() => {
    if (!selected || !available) return;
    const my = ++asgRef.current;
    setBusy(true);
    const id = window.setTimeout(() => {
      api.assign(selected, policy)
        .then((r) => { if (asgRef.current === my) { setPreview(r); setError(null); } })
        .catch((e) => { if (asgRef.current === my) setError(String(e)); })
        .finally(() => { if (asgRef.current === my) setBusy(false); });
    }, 600);
    return () => window.clearTimeout(id);
  }, [selected, policy, available]);

  const importFile = async (f: File) => {
    setNote(null);
    try {
      const doc = JSON.parse(await f.text());
      const r = await api.importGrid(doc, f.name.replace(/\.json$/i, ""));
      await refreshGrids();
      onSelect(r.id);
      setNote(t("netz.imported", { name: r.name }));
    } catch (e) {
      setNote(`${t("netz.importErr")} ${String(e)}`);
    }
  };

  const apply = (withLoadgen: boolean) => {
    if (!selected) return;
    setBusy(true);
    api.apply(selected, withLoadgen ? policy : undefined)
      .then((r) => onApplied(r.active))
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };

  const toggle = (id: string) =>
    setChosen((s) => {
      const n = new Set(s);
      n.has(id) ? n.delete(id) : n.add(id);
      return n;
    });

  // transformer loading from the net curve: S = P/cosphi vs. rated sn
  const netKw = preview ? preview.net_p_mw.map((p) => p * 1000) : [];
  const grossKw = preview ? preview.total_load_p_mw.map((p) => p * 1000) : [];
  const hasPv = !!preview && preview.n_pv > 0;
  const sn = preview?.trafo_sn_mva ?? null;
  const peakAbsMw = preview ? Math.max(preview.peak_net_mw, -preview.min_net_mw) : 0;
  const trafoPct = preview && sn ? (peakAbsMw / pf / sn) * 100 : null;
  const ratingKw = sn ? sn * pf * 1000 : undefined;   // P-equivalent chart line

  if (error && !grids) return <div className="empty">{t("grid.failed")}<br />{error}</div>;
  if (!grids) return <div className="spinner">{t("grid.loadingLib")}</div>;

  return (
    <div className="netzstudio">
      {/* ---- 1 · pick or import a grid ---------------------------------- */}
      <aside className="ns-list">
        <h3>{t("netz.step1")}</h3>
        <button className="ghost" style={{ width: "100%" }}
                onClick={() => fileRef.current?.click()}>
          ⬆ {t("netz.import")}
        </button>
        <input ref={fileRef} type="file" accept=".json,application/json"
               style={{ display: "none" }}
               onChange={(e) => {
                 const f = e.target.files?.[0];
                 if (f) importFile(f);
                 e.target.value = "";
               }} />
        {note && <p className="note" style={{ fontSize: "0.75rem" }}>{note}</p>}

        <div className="ns-hdr">{t("netz.library")}</div>
        {library.map((g) => (
          <GridRow key={g.id} g={g} selected={g.id === selected} onClick={() => onSelect(g.id)} />
        ))}
        {library.length === 0 && <div className="muted ns-empty">—</div>}

        <div className="ns-hdr">{t("netz.own")}</div>
        {own.map((g) => (
          <GridRow key={g.id} g={g} selected={g.id === selected} onClick={() => onSelect(g.id)} />
        ))}
        {own.length === 0 && <div className="muted ns-empty">{t("netz.noOwn")}</div>}
      </aside>

      {/* ---- 2 · loads & generation ------------------------------------- */}
      <section className="ns-config">
        <h3>{t("netz.step2")}</h3>
        {!selected && <div className="muted">{t("netz.pickHint")}</div>}
        {selected && (
          <>
            {gridPrev && (
              <div className="kpis">
                <Kpi k={t("grid.kBuses")} v={`${gridPrev.n_bus}`} />
                <Kpi k={t("grid.kLines")} v={`${gridPrev.n_line}`} />
                <Kpi k={t("grid.kLoads")} v={`${gridPrev.n_load}`} />
                {gridPrev.trafos[0] && gridPrev.trafos[0].sn_mva > 0 && (
                  <Kpi k={t("netz.kTrafo")} v={`${fmt(gridPrev.trafos[0].sn_mva * 1000, 0)} kVA`} />
                )}
              </div>
            )}
            {!available && <p className="note">{t("loads.noLpg")}</p>}

            <div className="field">
              <label>{t("loads.evPen", { pct: Math.round(evPen * 100) })}</label>
              <input type="range" min={0} max={1} step={0.05} value={evPen}
                     onChange={(e) => setEvPen(+e.target.value)} />
              <span className="muted" style={{ fontSize: "0.72rem" }}>{t("loads.evHint")}</span>
            </div>
            {evPen > 0 && (
              <div className="field">
                <label>{t("loads.wallbox")}</label>
                <select value={evKw} onChange={(e) => setEvKw(+e.target.value)}>
                  <option value={3.7}>{t("loads.ev37")}</option>
                  <option value={11}>{t("loads.ev11")}</option>
                  <option value={22}>{t("loads.ev22")}</option>
                </select>
              </div>
            )}
            <div className="field">
              <label>{t("loads.pvPen", { pct: Math.round(pvPen * 100) })}</label>
              <input type="range" min={0} max={1} step={0.05} value={pvPen}
                     onChange={(e) => setPvPen(+e.target.value)} />
            </div>
            {pvPen > 0 && (
              <div className="field">
                <label>{t("loads.pvSize", { kwp: pvKwp.toFixed(1) })}</label>
                <input type="range" min={1} max={15} step={0.5} value={pvKwp}
                       onChange={(e) => setPvKwp(+e.target.value)} />
              </div>
            )}
            <div className="field">
              <label className="arch-row" style={{ padding: 0 }}>
                <input type="checkbox" checked={mfh} onChange={() => setMfh(!mfh)} />
                <span style={{ flex: 1 }}>{t("netz.mfh")}</span>
              </label>
              <span className="muted" style={{ fontSize: "0.72rem" }}>{t("netz.mfhHint")}</span>
            </div>

            <details>
              <summary className="muted" style={{ cursor: "pointer", fontSize: "0.82rem" }}>
                {t("netz.advanced")}
              </summary>
              {archetypes && available && (
                <div className="field">
                  <label>{t("loads.archetypes", { count: chosen.size })}</label>
                  <div className="arch-list">
                    {archetypes.map((a) => (
                      <label className="arch-row" key={a.id}>
                        <input type="checkbox" checked={chosen.has(a.id)} onChange={() => toggle(a.id)} />
                        <span style={{ flex: 1 }}>{t(`arch.${a.id}`, { defaultValue: a.label })}</span>
                        <span className="muted">{t("loads.kwhYr", { kwh: fmt(a.annual_kwh, 0) })}</span>
                      </label>
                    ))}
                  </div>
                </div>
              )}
              <div className="field">
                <label>{t("loads.mode")}</label>
                <select value={mode} onChange={(e) => setMode(e.target.value as "round_robin" | "random")}>
                  <option value="round_robin">{t("loads.roundRobin")}</option>
                  <option value="random">{t("loads.random")}</option>
                </select>
              </div>
              <div className="field">
                <label>{t("loads.seed")}</label>
                <input type="number" value={seed} onChange={(e) => setSeed(+e.target.value)} />
              </div>
              <div className="field">
                <label>{t("loads.scale", { scale: scale.toFixed(2) })}</label>
                <input type="range" min={0.2} max={3} step={0.1} value={scale}
                       onChange={(e) => setScale(+e.target.value)} />
              </div>
              <div className="field">
                <label>{t("loads.pf", { pf: pf.toFixed(2) })}</label>
                <input type="range" min={0.8} max={1} step={0.01} value={pf}
                       onChange={(e) => setPf(+e.target.value)} />
              </div>
            </details>
          </>
        )}
      </section>

      {/* ---- 3 · check & start ------------------------------------------ */}
      <section className="ns-preview">
        <h3>{t("netz.step3")}</h3>
        {!selected && <div className="muted">{t("netz.pickHint")}</div>}
        {selected && gridPrev && (
          <div style={{ marginBottom: "0.7rem" }}>
            <StructureDiagram p={gridPrev} />
          </div>
        )}
        {selected && busy && !preview && <div className="spinner">…</div>}
        {selected && preview && (
          <>
            <div className="kpis">
              <Kpi k={t("netz.kHouseholds")} v={`${preview.n_households}`} />
              <Kpi k={t("loads.kEvs")} v={`${preview.n_ev}`} />
              <Kpi k={t("loads.kPv")} v={`${preview.n_pv}`} />
              <Kpi k={t("loads.kNetPeak")} v={`${fmt(peakAbsMw * 1000, 1)} kW`} />
              {trafoPct != null && (
                <Kpi k={t("netz.kTrafoPeak")} v={`${fmt(trafoPct, 0)} %`}
                     tone={trafoPct >= 100 ? "bad" : trafoPct >= 80 ? "warn" : "ok"} />
              )}
            </div>
            {preview.n_mfh > 0 && (
              <p className="muted" style={{ fontSize: "0.78rem" }}>
                🏢 {t("netz.mfhUsed", { mfh: preview.n_mfh, hh: preview.n_households })}
              </p>
            )}
            <Sparkline values={netKw} overlay={hasPv ? grossKw : undefined}
                       width={560} height={230} marker={ratingKw} />
            <p className="muted" style={{ fontSize: "0.78rem" }}>
              {t("loads.netDesc")}{hasPv && t("loads.netDescPv")}.{" "}
              {sn
                ? t("netz.trafoLine", { kva: fmt(sn * 1000, 0) })
                : t("netz.noTrafoRating")}
              {preview.min_net_mw < 0 && <span className="note"> {t("loads.reverseFlow")}</span>}
            </p>
            <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.6rem" }}>
              <button className="primary" onClick={() => apply(available)} disabled={busy}>
                {t("loads.applyRun")}
              </button>
              {available && (
                <button className="ghost" onClick={() => apply(false)} disabled={busy}
                        title={t("loads.placeholderHint")}>
                  {t("loads.placeholder")}
                </button>
              )}
            </div>
            {error && <p className="note">{error}</p>}
          </>
        )}
      </section>
    </div>
  );
}

function GridRow({ g, selected, onClick }: { g: GridListItem; selected: boolean; onClick: () => void }) {
  const { t } = useTranslation();
  return (
    <div className={`ns-row${selected ? " sel" : ""}`} onClick={onClick}>
      <div className="title">{gridDisplayName(g.id, g.name, t)}</div>
      <div className="sub">
        <span className="tag">{g.voltage}</span>
        {g.character && g.character !== "user" && (
          <span className="tag">{t(`grid.${g.character}`)}</span>
        )}
        {g.nodes != null && <span className="muted"> {t("grid.nodes", { count: g.nodes })}</span>}
      </div>
    </div>
  );
}

function Kpi({ k, v, tone }: { k: string; v: string; tone?: "ok" | "warn" | "bad" }) {
  const color = tone === "bad" ? "#e5534b" : tone === "warn" ? "#f2ae00"
    : tone === "ok" ? "#3fb950" : undefined;
  return (
    <div className="kpi">
      <div className="v" style={color ? { color } : undefined}>{v}</div>
      <div className="k">{k}</div>
    </div>
  );
}
