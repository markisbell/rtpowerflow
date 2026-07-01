import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { EngineStatus, Topology } from "../types";
import { useStepStream } from "../useWebSocket";
import { fmt, loadingColor, voltageColor } from "../scales";
import GridDiagram from "../components/GridDiagram";
import MapDiagram from "../components/MapDiagram";
import NodeProfile from "../components/NodeProfile";
import LineProfile from "../components/LineProfile";
import TrafoProfile from "../components/TrafoProfile";

type Layout = "map" | "tree";

export default function LivePowerFlow({ onActive }: { onActive: () => void }) {
  const [topo, setTopo] = useState<Topology | null>(null);
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>("tree");
  const [showValues, setShowValues] = useState(false);
  const [selectedBus, setSelectedBus] = useState<number | null>(null);
  const [selectedLine, setSelectedLine] = useState<number | null>(null);
  const [selectedTrafo, setSelectedTrafo] = useState<number | null>(null);
  const layoutInit = useRef(false);

  // node / line / trafo selections are mutually exclusive — one graph panel at a time
  const selectBus = (b: number) => { setSelectedLine(null); setSelectedTrafo(null); setSelectedBus(b); };
  const selectLine = (l: number) => { setSelectedBus(null); setSelectedTrafo(null); setSelectedLine(l); };
  const selectTrafo = (t: number) => { setSelectedBus(null); setSelectedLine(null); setSelectedTrafo(t); };
  const { latest, status: wsStatus } = useStepStream(true);

  const loadTopo = () => api.network().then(setTopo).catch((e) => setError(String(e)));
  const loadStatus = () => api.status().then(setStatus).catch(() => {});

  useEffect(() => {
    loadTopo();
    loadStatus();
    onActive();
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

  // drop a stale node/line/trafo selection when the grid changes
  useEffect(() => { setSelectedBus(null); setSelectedLine(null); setSelectedTrafo(null); }, [topo?.name]);

  const toggleRun = async () => {
    setStatus(status?.running ? await api.pause() : await api.start());
  };
  const seek = async (step: number) => setStatus(await api.seek(step));

  if (error) return <div className="empty">Failed to load network:<br />{error}</div>;
  if (!topo) return <div className="spinner">Loading network…</div>;

  const s = latest?.summary;
  const step = latest?.step ?? status?.step ?? 0;
  const spd = topo.steps_per_day || status?.steps_per_day || 1440;

  return (
    <div className="live">
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
          <MapDiagram topo={topo} latest={latest} onSelectBus={selectBus}
                      onSelectLine={selectLine} onSelectTrafo={selectTrafo} />
        ) : (
          <GridDiagram topo={topo} latest={latest} showValues={showValues}
                       onSelectBus={selectBus} selectedBus={selectedBus}
                       onSelectLine={selectLine} selectedLine={selectedLine}
                       onSelectTrafo={selectTrafo} selectedTrafo={selectedTrafo} />
        )}
      </div>

      <aside className="side">
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

        {selectedBus != null && (
          <NodeProfile
            bus={selectedBus}
            name={topo.buses.find((b) => b.id === selectedBus)?.name ?? String(selectedBus)}
            onClose={() => setSelectedBus(null)}
          />
        )}
        {selectedLine != null && (
          <LineProfile
            line={selectedLine}
            name={topo.lines.find((l) => l.id === selectedLine)?.name ?? String(selectedLine)}
            onClose={() => setSelectedLine(null)}
          />
        )}
        {selectedTrafo != null && (
          <TrafoProfile
            trafo={selectedTrafo}
            name={topo.trafos.find((t) => t.id === selectedTrafo)?.name ?? String(selectedTrafo)}
            onClose={() => setSelectedTrafo(null)}
          />
        )}
        {selectedBus == null && selectedLine == null && selectedTrafo == null && (
          <p className="muted" style={{ fontSize: "0.72rem", marginTop: "0.5rem" }}>
            Click a node for its load / generation / voltage, a line for its current, or the
            transformer for its power exchange — each over the day.
          </p>
        )}

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
        <span className="clock" style={{ minWidth: 64 }}>
          {latest?.time_of_day ?? "00:00"}
        </span>
        <input
          type="range"
          min={0}
          max={spd - 1}
          value={step}
          onChange={(e) => seek(+e.target.value)}
        />
        <span className="muted" style={{ minWidth: 90, textAlign: "right" }}>
          step {step}/{spd}
        </span>
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
