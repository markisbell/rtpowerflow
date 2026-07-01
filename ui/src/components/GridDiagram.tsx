import { useEffect, useMemo, useRef, useState } from "react";
import type { StepResult, Topology } from "../types";
import { currentWidth, loadingColor, voltageColor, fmt } from "../scales";

// stacked-feeder layout spacing (user units): horizontal per depth level, and the
// vertical room for a feeder track + each leaf-load stub fanned below its node.
const COL = 96;
const LEAF_STEP = 28;
const TRACK_BASE = 34;
const TRACK_GAP = 16;
const PADX = 48;
const PADY = 36;

interface Props {
  topo: Topology;
  latest: StepResult | null;
  showValues?: boolean;
  onSelectBus?: (bus: number) => void;
  selectedBus?: number | null;
  onSelectLine?: (line: number) => void;
  selectedLine?: number | null;
  onSelectTrafo?: (trafo: number) => void;
  selectedTrafo?: number | null;
}

interface Tip {
  x: number;
  y: number;
  lines: string[];
}

type XY = { x: number; y: number };

export default function GridDiagram({ topo, latest, showValues = false, onSelectBus, selectedBus = null, onSelectLine, selectedLine = null, onSelectTrafo, selectedTrafo = null }: Props) {
  // ---- stacked horizontal feeder layout ------------------------------------
  // x = depth from the slack (feeders run straight left→right, using the width);
  // the largest child continues its parent's track, other feeders drop to a new
  // track, and leaf loads fan as short vertical stubs off their trunk node.
  const { pos, parent, depthOf, W, H } = useMemo(() => {
    const ids = new Set(topo.buses.map((b) => b.id));
    const adj = new Map<number, number[]>();
    const ensure = (n: number) => { let a = adj.get(n); if (!a) { a = []; adj.set(n, a); } return a; };
    topo.buses.forEach((b) => ensure(b.id));
    topo.lines.forEach((l) => {
      if (l.in_service === false) return; // normally-open ties are not tree edges
      if (ids.has(l.from_bus) && ids.has(l.to_bus)) { ensure(l.from_bus).push(l.to_bus); ensure(l.to_bus).push(l.from_bus); }
    });
    topo.trafos.forEach((t) => {
      if (ids.has(t.hv_bus) && ids.has(t.lv_bus)) { ensure(t.hv_bus).push(t.lv_bus); ensure(t.lv_bus).push(t.hv_bus); }
    });

    const depth = new Map<number, number>();
    const parent = new Map<number, number>();
    const kids = new Map<number, number[]>();
    const seen = new Set<number>();
    const roots: number[] = [];
    const bfs = (r: number) => {
      roots.push(r); seen.add(r); depth.set(r, 0); kids.set(r, []);
      const q = [r];
      while (q.length) {
        const u = q.shift()!;
        for (const v of [...(adj.get(u) ?? [])].sort((a, b) => a - b)) {
          if (!seen.has(v)) { seen.add(v); parent.set(v, u); depth.set(v, (depth.get(u) ?? 0) + 1); kids.set(v, []); kids.get(u)!.push(v); q.push(v); }
        }
      }
    };
    topo.ext_grids.map((e) => e.bus).filter((b) => ids.has(b)).forEach((r) => { if (!seen.has(r)) bfs(r); });
    topo.buses.forEach((b) => { if (!seen.has(b.id)) bfs(b.id); }); // leftover components

    // subtree sizes → the largest child stays on the trunk (a straight feeder)
    const size = new Map<number, number>();
    const computeSize = (n: number): number => { let s = 1; for (const c of kids.get(n) ?? []) s += computeSize(c); size.set(n, s); return s; };
    roots.forEach((r) => computeSize(r));
    const isLeaf = (n: number) => (kids.get(n) ?? []).length === 0;

    const track = new Map<number, number>();
    const leafKids = new Map<number, number[]>();
    const trackFan = new Map<number, number>(); // busiest leaf-fan on each track
    let cursor = 0;
    const place = (n: number, t: number) => {
      track.set(n, t);
      const ch = kids.get(n) ?? [];
      const internal = ch.filter((c) => !isLeaf(c)).sort((a, b) => (size.get(b)! - size.get(a)!) || (a - b));
      const leaves = ch.filter(isLeaf).sort((a, b) => a - b);
      leafKids.set(n, leaves);
      if (leaves.length) trackFan.set(t, Math.max(trackFan.get(t) ?? 0, leaves.length));
      internal.forEach((c, i) => { if (i === 0) place(c, t); else { cursor += 1; place(c, cursor); } });
    };
    roots.forEach((r) => { if (!track.has(r)) { place(r, cursor); cursor += 1; } });

    const nTracks = Math.max(1, Math.max(0, ...track.values()) + 1);
    const trackY: number[] = [];
    let acc = PADY;
    for (let t = 0; t < nTracks; t++) { trackY[t] = acc; acc += TRACK_BASE + (trackFan.get(t) ?? 0) * LEAF_STEP + TRACK_GAP; }
    const H = acc + PADY;
    const maxDepth = Math.max(1, ...depth.values());
    const W = PADX * 2 + maxDepth * COL;

    const pos = new Map<number, XY>();
    for (const [n, t] of track) pos.set(n, { x: PADX + (depth.get(n) ?? 0) * COL, y: trackY[t] });
    for (const [p, ls] of leafKids) {
      const pp = pos.get(p); if (!pp) continue;
      const lx = PADX + ((depth.get(p) ?? 0) + 1) * COL;
      ls.forEach((c, j) => pos.set(c, { x: lx, y: pp.y + (j + 1) * LEAF_STEP }));
    }
    return { pos, parent, depthOf: depth, W, H };
  }, [topo]);

  // orthogonal edge: vertical riser at the parent's x, then a horizontal tap to
  // the child (a straight horizontal line when they share a track).
  const edge = (aId: number, bId: number): { p: XY; c: XY; d: string } | null => {
    const A = pos.get(aId); const B = pos.get(bId);
    if (!A || !B) return null;
    let p = A; let c = B;
    if (parent.get(aId) === bId) { p = B; c = A; }
    else if (parent.get(bId) === aId) { p = A; c = B; }
    else if ((depthOf.get(bId) ?? 0) < (depthOf.get(aId) ?? 0)) { p = B; c = A; }
    return { p, c, d: `M ${p.x} ${p.y} V ${c.y} H ${c.x}` };
  };

  const [vb, setVb] = useState({ x: 0, y: 0, w: W, h: H });
  const [tip, setTip] = useState<Tip | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);

  // re-frame to the whole diagram when the canvas size changes (grid swap)
  useEffect(() => setVb({ x: 0, y: 0, w: W, h: H }), [W, H]);

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
  const liveBusFull = useMemo(() => {
    const m = new Map<number, StepResult["buses"][number]>();
    latest?.buses.forEach((b) => m.set(b.index, b));
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
        {/* lines (electrical overlay, orthogonal feeder routing) */}
        {topo.lines.map((ln) => {
          const e = edge(ln.from_bus, ln.to_bus);
          if (!e) return null;
          const live = liveLine.get(ln.id);
          const color = loadingColor(live?.loading_percent);
          const wdt = live ? currentWidth(live.i_ka, maxIka) : 1.5;
          const rev = (live?.p_from_mw ?? 0) < 0;
          const sel = ln.id === selectedLine;
          const open = ln.in_service === false;
          return (
            <g
              key={`l${ln.id}`}
              data-line={ln.id}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectLine?.(ln.id)}
              onMouseEnter={(ev) =>
                showTip(ev, [
                  `Line ${ln.name ?? ln.id}`,
                  `loading ${fmt(live?.loading_percent, 1)} %`,
                  `I ${fmt(live?.i_ka != null ? live.i_ka * 1000 : null, 1)} A`,
                  `P ${fmt(live?.p_from_mw != null ? live.p_from_mw * 1000 : null, 1)} kW`,
                  "click → current graph",
                ])
              }
            >
              {sel && <path d={e.d} fill="none" stroke="#ffd166" strokeWidth={wdt + 5} strokeLinejoin="round" strokeLinecap="round" opacity={0.5} />}
              {/* wide transparent hit area so thin feeders are easy to click */}
              <path d={e.d} fill="none" stroke="transparent" strokeWidth={Math.max(wdt, 10)} />
              <path
                d={e.d}
                fill="none"
                stroke={open ? "#888" : color}
                strokeWidth={open ? 1.5 : wdt}
                strokeLinejoin="round"
                strokeLinecap="round"
                strokeDasharray={open ? "5 7" : undefined}
                className={!open && animate && live && Math.abs(live.p_from_mw) > 1e-4 ? `flow${rev ? " rev" : ""}` : ""}
              />
            </g>
          );
        })}

        {/* transformers */}
        {topo.trafos.map((tr) => {
          const e = edge(tr.hv_bus, tr.lv_bus);
          if (!e) return null;
          const live = liveTrafo.get(tr.id);
          const mx = (e.p.x + e.c.x) / 2;
          const my = e.c.y;
          const color = loadingColor(live?.loading_percent);
          const sel = tr.id === selectedTrafo;
          return (
            <g
              key={`t${tr.id}`}
              data-trafo={tr.id}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectTrafo?.(tr.id)}
              onMouseEnter={(ev) =>
                showTip(ev, [
                  `Trafo ${tr.name ?? tr.id}`,
                  `${fmt(tr.sn_mva * 1000, 0)} kVA`,
                  `loading ${fmt(live?.loading_percent, 1)} %`,
                  "click → power graph",
                ])
              }
            >
              <path d={e.d} fill="none" stroke={color} strokeWidth={6} strokeLinejoin="round" strokeLinecap="round" />
              <circle cx={mx} cy={my} r={sel ? 13 : 11} fill="#0b0d11"
                stroke={sel ? "#ffd166" : color} strokeWidth={sel ? 4 : 3} />
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
              data-bus={bus.id}
              cx={p.x}
              cy={p.y}
              r={bus.id === selectedBus ? (isExt ? 7 : 5) : isExt ? 6 : 3}
              fill={isExt ? "#e6e6e6" : voltageColor(vm)}
              stroke={bus.id === selectedBus ? "#ffd166" : isExt ? "#7fd1ff" : "none"}
              strokeWidth={bus.id === selectedBus ? 2.5 : isExt ? 2 : 0}
              style={{ cursor: "pointer" }}
              onClick={() => onSelectBus?.(bus.id)}
              onMouseEnter={(e) =>
                showTip(e, [
                  `${isExt ? "Slack " : "Bus "}${bus.name}`,
                  `${bus.vn_kv} kV`,
                  `Vm ${fmt(vm, 4)} pu`,
                  "click → load/gen graph",
                ])
              }
            />
          );
        })}

        {/* SCADA value indicators: current on lines, voltage + power at nodes */}
        {showValues && latest && (
          <g pointerEvents="none">
            {topo.lines.map((ln) => {
              const e = edge(ln.from_bus, ln.to_bus);
              const live = liveLine.get(ln.id);
              if (!e || !live) return null;
              return (
                <ValueBox key={`vl${ln.id}`} x={(e.p.x + e.c.x) / 2} y={e.c.y}
                  rows={[`${fmt(live.i_ka * 1000, 0)} A`]} />
              );
            })}
            {topo.buses.map((bus) => {
              const p = pos.get(bus.id);
              const lb = liveBusFull.get(bus.id);
              if (!p || !lb) return null;
              const rows = [`${fmt(lb.vm_pu, 3)} pu`];
              if (Math.abs(lb.p_mw) > 1e-4) rows.push(`${fmt(lb.p_mw * 1000, 0)} kW`);
              return <ValueBox key={`vb${bus.id}`} x={p.x + 7} y={p.y} rows={rows} accent />;
            })}
          </g>
        )}
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

// A small SCADA-style value box (dark rectangle + monospace reading) drawn in SVG
// user units, so it scales with zoom — like the indicators on a control-room board.
function ValueBox({ x, y, rows, accent }: { x: number; y: number; rows: string[]; accent?: boolean }) {
  const w = Math.max(...rows.map((r) => r.length)) * 5.4 + 6;
  const h = rows.length * 10 + 3;
  return (
    <g transform={`translate(${x + 2},${y - h / 2})`}>
      <rect width={w} height={h} rx={2} fill="#0b0d11" opacity={0.82} stroke="#33414f" strokeWidth={0.4} />
      {rows.map((r, i) => (
        <text key={i} x={3} y={9 + i * 10} fontSize={8.5}
          style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}
          fill={accent ? "#8fe3c8" : "#74c0fc"}>
          {r}
        </text>
      ))}
    </g>
  );
}
