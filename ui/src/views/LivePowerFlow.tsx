import { useEffect, useState } from "react";
import { api } from "../api";
import type { EngineStatus, Topology } from "../types";
import { useStepStream } from "../useWebSocket";
import { fmt, loadingColor, voltageColor } from "../scales";
import GridDiagram from "../components/GridDiagram";

export default function LivePowerFlow({ onActive }: { onActive: () => void }) {
  const [topo, setTopo] = useState<Topology | null>(null);
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [layout, setLayout] = useState<"force" | "tree">("force");
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
          <button className={layout === "force" ? "on" : ""} onClick={() => setLayout("force")}>
            Geographic
          </button>
          <button className={layout === "tree" ? "on" : ""} onClick={() => setLayout("tree")}>
            Schematic
          </button>
        </div>
        <GridDiagram topo={topo} latest={latest} layout={layout} />
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
        <p className="muted" style={{ fontSize: "0.72rem", marginTop: "0.6rem" }}>
          Line width ∝ current. Scroll to zoom, drag to pan.
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
