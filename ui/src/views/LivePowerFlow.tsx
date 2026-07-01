import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Battery, BatteryMode, EngineStatus, Topology } from "../types";
import { useStepStream } from "../useWebSocket";
import { fmt, loadingColor, voltageColor } from "../scales";
import GridDiagram from "../components/GridDiagram";
import MapDiagram from "../components/MapDiagram";
import NodeProfile from "../components/NodeProfile";
import LineProfile from "../components/LineProfile";
import TrafoProfile from "../components/TrafoProfile";
import BatteryPanel from "../components/BatteryPanel";

type Layout = "map" | "tree";
type SelKind = "bus" | "line" | "trafo";
interface Sel { kind: SelKind; id: number; }
const MAX_SEL = 4;

export default function LivePowerFlow({ onActive }: { onActive: () => void }) {
  const [topo, setTopo] = useState<Topology | null>(null);
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>("tree");
  const [showValues, setShowValues] = useState(false);
  const [selection, setSelection] = useState<Sel[]>([]);   // up to MAX_SEL marked elements
  const [stepSeconds, setStepSeconds] = useState(1);   // accelerated-tick interval (s/step)
  const [pvDates, setPvDates] = useState<string[]>([]); // real-PV day calendar (day slider)
  const [sideW, setSideW] = useState(340);             // resizable overview width (px)
  const [batteries, setBatteries] = useState<Battery[]>([]);
  const [batModes, setBatModes] = useState<BatteryMode[]>([]);
  const [batHasPrices, setBatHasPrices] = useState(false);
  const layoutInit = useRef(false);
  const intervalInit = useRef(false);

  // ctrl/⌘-click marks several elements (stacked on the right, up to MAX_SEL); a
  // plain click selects one; clicking a marked element again removes it.
  const toggleSelect = (kind: SelKind, id: number, additive: boolean) =>
    setSelection((prev) => {
      const i = prev.findIndex((x) => x.kind === kind && x.id === id);
      if (additive) {
        if (i >= 0) return prev.filter((_, j) => j !== i);
        return [...prev, { kind, id }].slice(-MAX_SEL);
      }
      if (i >= 0 && prev.length === 1) return [];
      return [{ kind, id }];
    });
  const selectBus = (id: number, add: boolean) => toggleSelect("bus", id, add);
  const selectLine = (id: number, add: boolean) => toggleSelect("line", id, add);
  const selectTrafo = (id: number, add: boolean) => toggleSelect("trafo", id, add);
  const unselect = (kind: SelKind, id: number) =>
    setSelection((prev) => prev.filter((x) => !(x.kind === kind && x.id === id)));
  const { latest, status: wsStatus } = useStepStream(true);

  const loadTopo = () => api.network().then(setTopo).catch((e) => setError(String(e)));
  const loadStatus = () => api.status().then(setStatus).catch(() => {});
  const reloadBatteries = () => api.batteries()
    .then((r) => { setBatteries(r.batteries); setBatModes(r.modes); setBatHasPrices(r.has_prices); })
    .catch(() => {});

  useEffect(() => {
    loadTopo();
    loadStatus();
    onActive();
    api.pvDays().then((r) => setPvDates(r.dates)).catch(() => {});
    reloadBatteries();
    const t = setInterval(loadStatus, 2000);
    return () => clearInterval(t);
  }, []);

  // default to the real OSM map for grids that carry geo-coordinates, else schematic
  useEffect(() => {
    if (topo && !layoutInit.current) {
      layoutInit.current = true;
      setLayout(topo.has_geo ? "map" : "tree");
      setShowValues(topo.buses.length <= 40);   // on by default for small grids; toggle for big ones
    }
  }, [topo]);

  // drop a stale selection when the grid changes; batteries reset with the grid
  useEffect(() => { setSelection([]); reloadBatteries(); }, [topo?.name]);

  // adopt the engine's current tick interval once, then it's user-driven
  useEffect(() => {
    if (status && !intervalInit.current) { intervalInit.current = true; setStepSeconds(status.interval_seconds); }
  }, [status]);

  const toggleRun = async () => {
    setStatus(status?.running ? await api.pause() : await api.start());
  };
  const seek = async (step: number) => setStatus(await api.seek(step));
  const changeInterval = async (s: number) => { setStepSeconds(s); setStatus(await api.stepInterval(s)); };
  const seekDay = async (d: number) => setStatus(await api.seekDay(d));
  const addBattery = async (bus: number, capacity_kwh: number, power_kw: number, mode: BatteryMode) => {
    try { await api.addBattery({ bus, capacity_kwh, power_kw, mode }); } finally { reloadBatteries(); }
  };
  const removeBattery = async (idx: number) => {
    try { await api.removeBattery(idx); } finally { reloadBatteries(); }
  };

  if (error) return <div className="empty">Failed to load network:<br />{error}</div>;
  if (!topo) return <div className="spinner">Loading network…</div>;

  const s = latest?.summary;
  const step = latest?.step ?? status?.step ?? 0;
  const spd = topo.steps_per_day || status?.steps_per_day || 1440;
  // current time of day as a 0..1 fraction, for the "now" marker on the graphs
  const nowFrac = latest && spd > 1 ? latest.step / (spd - 1) : null;
  // real-PV day: use the per-step value while running, else the (seek-updated) status
  const nDays = status?.n_days ?? 1;
  const curDay = (latest && status?.running) ? latest.day : (status?.day ?? latest?.day ?? 0);
  const dayIdx = nDays > 0 ? ((curDay % nDays) + nDays) % nDays : 0;

  const selBuses = selection.filter((x) => x.kind === "bus").map((x) => x.id);
  const selLines = selection.filter((x) => x.kind === "line").map((x) => x.id);
  const selTrafos = selection.filter((x) => x.kind === "trafo").map((x) => x.id);

  // where "add battery" targets: a single selected node, or the transformer's LV busbar
  let addBus: number | null = null;
  let addIsTrafo = false;
  if (selection.length === 1) {
    const one = selection[0];
    if (one.kind === "bus") addBus = one.id;
    else if (one.kind === "trafo") {
      const tr = topo.trafos.find((t) => t.id === one.id);
      if (tr) { addBus = tr.lv_bus; addIsTrafo = true; }
    }
  }
  const batLive: Record<number, { soc_percent: number; p_mw: number }> = {};
  latest?.batteries?.forEach((b) => { batLive[b.index] = { soc_percent: b.soc_percent, p_mw: b.p_mw }; });
  const batteryBuses = batteries.map((b) => b.bus);

  // drag the panel's left edge to widen it (and the graphs, which are width:100%)
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const move = (ev: MouseEvent) => setSideW(Math.min(760, Math.max(260, window.innerWidth - ev.clientX)));
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  return (
    <div className="live" style={{ gridTemplateColumns: `1fr ${sideW}px` }}>
      <div className="diagram-wrap">
        <div className="layout-toggle">
          {topo.has_geo && (
            <button className={layout === "map" ? "on" : ""} onClick={() => setLayout("map")}
                    title="Real OpenStreetMap basemap at the grid's coordinates">
              🗺 Map
            </button>
          )}
          <button className={layout === "tree" ? "on" : ""} onClick={() => setLayout("tree")}>
            Schematic
          </button>
          {layout === "tree" && (
            <button
              className={showValues ? "on" : ""}
              style={{ marginLeft: 6 }}
              onClick={() => setShowValues((v) => !v)}
              title="Show live current on lines and voltage/power at nodes (SCADA-style readings)"
            >
              Values
            </button>
          )}
        </div>
        {layout === "map" ? (
          <MapDiagram topo={topo} latest={latest} batteryBuses={batteryBuses} onSelectBus={selectBus}
                      onSelectLine={selectLine} onSelectTrafo={selectTrafo} />
        ) : (
          <GridDiagram topo={topo} latest={latest} showValues={showValues} batteryBuses={batteryBuses}
                       onSelectBus={selectBus} selectedBuses={selBuses}
                       onSelectLine={selectLine} selectedLines={selLines}
                       onSelectTrafo={selectTrafo} selectedTrafos={selTrafos} />
        )}
      </div>

      <aside className="side">
        <div className="side-resizer" onMouseDown={startResize} title="Drag to resize" />
        <div className="clock">
          {latest ? `day ${latest.day} · ${latest.time_of_day}` : "—"}
          {latest && !latest.converged && <span className="note"> · not converged</span>}
        </div>
        <div className="muted" style={{ fontSize: "0.75rem", marginBottom: "0.6rem" }}>
          {topo.name} · {topo.buses.length} buses · ws {wsStatus}
        </div>

        <Stat label="V min / max" value={s ? `${fmt(s.vm_pu_min, 3)} / ${fmt(s.vm_pu_max, 3)} pu` : "—"} />
        <Stat
          label="max line loading"
          value={s ? `${fmt(s.max_line_loading_percent, 1)} %` : "—"}
          color={loadingColor(s?.max_line_loading_percent)}
        />
        <Stat
          label="max trafo loading"
          value={s?.max_trafo_loading_percent != null ? `${fmt(s.max_trafo_loading_percent, 1)} %` : "n/a"}
          color={loadingColor(s?.max_trafo_loading_percent)}
        />
        <Stat label="total load" value={s ? `${fmt(s.total_load_mw * 1000, 1)} kW` : "—"} />
        <Stat label="generation" value={s ? `${fmt(s.total_gen_mw * 1000, 1)} kW` : "—"} />
        <Stat label="slack (import)" value={s ? `${fmt(s.total_ext_grid_mw * 1000, 1)} kW` : "—"} />
        <Stat label="losses" value={s ? `${fmt(s.total_losses_mw * 1000, 2)} kW` : "—"} />
        <Stat label="solve time" value={latest ? `${fmt(latest.solve_ms, 1)} ms` : "—"} />

        {selection.map((sel) => {
          const key = `${sel.kind}${sel.id}`;
          if (sel.kind === "bus")
            return <NodeProfile key={key} bus={sel.id}
                     name={topo.buses.find((b) => b.id === sel.id)?.name ?? String(sel.id)}
                     now={nowFrac} day={curDay} onClose={() => unselect("bus", sel.id)} />;
          if (sel.kind === "line")
            return <LineProfile key={key} line={sel.id}
                     name={topo.lines.find((l) => l.id === sel.id)?.name ?? String(sel.id)}
                     now={nowFrac} day={curDay} onClose={() => unselect("line", sel.id)} />;
          return <TrafoProfile key={key} trafo={sel.id}
                   name={topo.trafos.find((t) => t.id === sel.id)?.name ?? String(sel.id)}
                   now={nowFrac} day={curDay} onClose={() => unselect("trafo", sel.id)} />;
        })}
        {selection.length === 0 && (
          <p className="muted" style={{ fontSize: "0.72rem", marginTop: "0.5rem" }}>
            Click a node (load / generation / voltage), a line (current) or the transformer
            (power) for its daily graph. Strg/⌘+Klick marks several (up to {MAX_SEL}).
          </p>
        )}

        <BatteryPanel
          batteries={batteries} live={batLive} modes={batModes} hasPrices={batHasPrices}
          addBus={addBus} addIsTrafo={addIsTrafo} onAdd={addBattery} onRemove={removeBattery}
        />

        {layout === "tree" && (
          <>
            <div className="legend">
              <span>
                <i className="swatch" style={{ background: loadingColor(10) }} /> &lt;50%
              </span>
              <span>
                <i className="swatch" style={{ background: loadingColor(65) }} /> &lt;80%
              </span>
              <span>
                <i className="swatch" style={{ background: loadingColor(90) }} /> &lt;100%
              </span>
              <span>
                <i className="swatch" style={{ background: loadingColor(120) }} /> overload
              </span>
            </div>
            <div className="legend">
              <span>
                <i className="swatch" style={{ background: voltageColor(0.9) }} /> under-V
              </span>
              <span>
                <i className="swatch" style={{ background: voltageColor(1.0) }} /> ok
              </span>
              <span>
                <i className="swatch" style={{ background: voltageColor(1.1) }} /> over-V
              </span>
            </div>
          </>
        )}
        <p className="muted" style={{ fontSize: "0.72rem", marginTop: "0.6rem" }}>
          {layout === "map"
            ? "Lines on a jet colormap by loading; nodes reddened by voltage Δ — like ding0's plot. Scroll to zoom, drag to pan."
            : "Line width ∝ current. Scroll to zoom, drag to pan."}
        </p>
      </aside>

      <div className="controls-bar">
        <button className="primary" onClick={toggleRun}>
          {status?.running ? "⏸ Pause" : "▶ Play"}
        </button>
        <span className="clock" style={{ minWidth: 180 }}>
          Zeitpunkt: {latest?.time_of_day ?? "00:00"} Uhr
        </span>
        <input
          type="range"
          min={0}
          max={spd - 1}
          value={step}
          onChange={(e) => seek(+e.target.value)}
        />
        {nDays > 1 && (
          <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}
                 title="Realer PV-Tag (Messdaten)">
            Tag
            <input
              type="range"
              min={0}
              max={nDays - 1}
              value={dayIdx}
              onChange={(e) => seekDay(+e.target.value)}
              style={{ flex: "0 0 90px", width: 90 }}
            />
            <span style={{ minWidth: 74, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              {pvDates[dayIdx] ?? `#${dayIdx + 1}`}
            </span>
          </label>
        )}
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}
               title="Realzeit pro Simulationsschritt">
          Schrittdauer
          <input
            type="range"
            min={0.1}
            max={1}
            step={0.1}
            value={stepSeconds}
            onChange={(e) => changeInterval(+e.target.value)}
            style={{ flex: "0 0 90px", width: 90 }}
          />
          <span style={{ minWidth: 34, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
            {stepSeconds.toFixed(1).replace(".", ",")} s
          </span>
        </label>
      </div>
    </div>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="stat-row">
      <span className="muted">{label}</span>
      <span className="v" style={color ? { color } : undefined}>
        {value}
      </span>
    </div>
  );
}
