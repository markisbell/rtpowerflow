import { useMemo, useRef, useState } from "react";
import type { StepResult, Topology } from "../types";
import { currentWidth, loadingColor, voltageColor, fmt } from "../scales";

const W = 1000;
const H = 640;
const M = 36;

interface Props {
  topo: Topology;
  latest: StepResult | null;
  layout?: "force" | "tree";
}

interface Tip {
  x: number;
  y: number;
  lines: string[];
}

export default function GridDiagram({ topo, latest, layout = "force" }: Props) {
  const [vb, setVb] = useState({ x: 0, y: 0, w: W, h: H });
  const [tip, setTip] = useState<Tip | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);

  // static pixel positions per bus id (force-directed or tidy-tree coordinates)
  const pos = useMemo(() => {
    const m = new Map<number, { x: number; y: number }>();
    for (const b of topo.buses) {
      const nx = layout === "tree" ? b.tx : b.x;
      const ny = layout === "tree" ? b.ty : b.y;
      m.set(b.id, { x: M + nx * (W - 2 * M), y: M + ny * (H - 2 * M) });
    }
    return m;
  }, [topo, layout]);

  // live lookups
  const liveLine = useMemo(() => {
    const m = new Map<number, StepResult["lines"][number]>();
    latest?.lines.forEach((l) => m.set(l.index, l));
    return m;
  }, [latest]);
  const liveTrafo = useMemo(() => {
    const m = new Map<number, StepResult["trafos"][number]>();
    latest?.trafos.forEach((t) => m.set(t.index, t));
    return m;
  }, [latest]);
  const liveBus = useMemo(() => {
    const m = new Map<number, number>();
    latest?.buses.forEach((b) => m.set(b.index, b.vm_pu));
    return m;
  }, [latest]);

  const maxIka = useMemo(() => Math.max(1e-6, ...(latest?.lines.map((l) => l.i_ka) ?? [0])), [latest]);
  const animate = topo.lines.length < 400;

  // ---- pan / zoom ----
  const toSvg = (clientX: number, clientY: number) => {
    const r = svgRef.current!.getBoundingClientRect();
    return {
      x: vb.x + ((clientX - r.left) / r.width) * vb.w,
      y: vb.y + ((clientY - r.top) / r.height) * vb.h,
    };
  };
  const onWheel = (e: React.WheelEvent) => {
    const f = e.deltaY < 0 ? 0.85 : 1.18;
    const p = toSvg(e.clientX, e.clientY);
    setVb((v) => ({
      x: p.x - (p.x - v.x) * f,
      y: p.y - (p.y - v.y) * f,
      w: v.w * f,
      h: v.h * f,
    }));
  };
  const onDown = (e: React.MouseEvent) => (drag.current = { x: e.clientX, y: e.clientY });
  const onMove = (e: React.MouseEvent) => {
    if (!drag.current) return;
    const r = svgRef.current!.getBoundingClientRect();
    const dx = ((e.clientX - drag.current.x) / r.width) * vb.w;
    const dy = ((e.clientY - drag.current.y) / r.height) * vb.h;
    drag.current = { x: e.clientX, y: e.clientY };
    setVb((v) => ({ ...v, x: v.x - dx, y: v.y - dy }));
  };
  const onUp = () => (drag.current = null);
  const reset = () => setVb({ x: 0, y: 0, w: W, h: H });

  const showTip = (e: React.MouseEvent, lines: string[]) => {
    const r = svgRef.current!.getBoundingClientRect();
    setTip({ x: e.clientX - r.left + 12, y: e.clientY - r.top + 12, lines });
  };

  const extBuses = new Set(topo.ext_grids.map((e) => e.bus));

  return (
    <>
      <svg
        ref={svgRef}
        width="100%"
        height="100%"
        viewBox={`${vb.x} ${vb.y} ${vb.w} ${vb.h}`}
        onWheel={onWheel}
        onMouseDown={onDown}
        onMouseMove={onMove}
        onMouseUp={onUp}
        onMouseLeave={() => {
          onUp();
          setTip(null);
        }}
        style={{ cursor: drag.current ? "grabbing" : "grab" }}
      >
        {/* lines */}
        {topo.lines.map((ln) => {
          const a = pos.get(ln.from_bus);
          const b = pos.get(ln.to_bus);
          if (!a || !b) return null;
          const live = liveLine.get(ln.id);
          const color = loadingColor(live?.loading_percent);
          const wdt = live ? currentWidth(live.i_ka, maxIka) : 1.5;
          const rev = (live?.p_from_mw ?? 0) < 0;
          return (
            <line
              key={`l${ln.id}`}
              x1={a.x}
              y1={a.y}
              x2={b.x}
              y2={b.y}
              stroke={color}
              strokeWidth={wdt}
              strokeLinecap="round"
              className={animate && live && Math.abs(live.p_from_mw) > 1e-4 ? `flow${rev ? " rev" : ""}` : ""}
              onMouseEnter={(e) =>
                showTip(e, [
                  `Line ${ln.name ?? ln.id}`,
                  `loading ${fmt(live?.loading_percent, 1)} %`,
                  `I ${fmt(live?.i_ka != null ? live.i_ka * 1000 : null, 1)} A`,
                  `P ${fmt(live?.p_from_mw != null ? live.p_from_mw * 1000 : null, 1)} kW`,
                ])
              }
            />
          );
        })}

        {/* transformers */}
        {topo.trafos.map((tr) => {
          const a = pos.get(tr.hv_bus);
          const b = pos.get(tr.lv_bus);
          if (!a || !b) return null;
          const live = liveTrafo.get(tr.id);
          const mx = (a.x + b.x) / 2;
          const my = (a.y + b.y) / 2;
          const color = loadingColor(live?.loading_percent);
          return (
            <g
              key={`t${tr.id}`}
              onMouseEnter={(e) =>
                showTip(e, [
                  `Trafo ${tr.name ?? tr.id}`,
                  `${fmt(tr.sn_mva * 1000, 0)} kVA`,
                  `loading ${fmt(live?.loading_percent, 1)} %`,
                ])
              }
            >
              <line x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={color} strokeWidth={6} strokeLinecap="round" />
              <circle cx={mx} cy={my} r={11} fill="#0b0d11" stroke={color} strokeWidth={3} />
              <text x={mx} y={my + 3} textAnchor="middle" fontSize="9" fill="#e6e6e6">
                {live ? Math.round(live.loading_percent) : "T"}
              </text>
            </g>
          );
        })}

        {/* buses */}
        {topo.buses.map((bus) => {
          const p = pos.get(bus.id);
          if (!p) return null;
          const isExt = extBuses.has(bus.id);
          const vm = liveBus.get(bus.id);
          return (
            <circle
              key={`b${bus.id}`}
              cx={p.x}
              cy={p.y}
              r={isExt ? 6 : 3}
              fill={isExt ? "#e6e6e6" : voltageColor(vm)}
              stroke={isExt ? "#7fd1ff" : "none"}
              strokeWidth={isExt ? 2 : 0}
              onMouseEnter={(e) =>
                showTip(e, [
                  `${isExt ? "Slack " : "Bus "}${bus.name}`,
                  `${bus.vn_kv} kV`,
                  `Vm ${fmt(vm, 4)} pu`,
                ])
              }
            />
          );
        })}
      </svg>

      <button className="ghost" style={{ position: "absolute", top: 10, right: 10 }} onClick={reset}>
        Reset view
      </button>
      {tip && (
        <div className="tooltip" style={{ left: tip.x, top: tip.y }}>
          {tip.lines.map((l, i) => (
            <div key={i} style={i === 0 ? { fontWeight: 700 } : { color: "var(--muted)" }}>
              {l}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
