import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { NodeMeasurement, StepResult, Topology, TrafoMeasurement } from "../types";
import { currentWidth, loadingColor, voltageColor, fmt, UNOBSERVED, UNOBSERVED_LINE, V_BASE } from "../scales";

// stacked-feeder layout spacing (user units): horizontal per depth level, and the
// vertical room for a feeder track + each leaf-load stub fanned below its node.
const COL = 96;
const LEAF_STEP = 34;
const TRACK_BASE = 44;
const TRACK_GAP = 28;
const PADX = 48;
const PADY = 36;
// risers leave their parent a little to the RIGHT of the node column, so a
// drop crossing intermediate tracks never runs through their nodes (which sit
// exactly on column multiples)
const RISER_DX = 18;

interface Props {
  topo: Topology;
  latest: StepResult | null;
  showValues?: boolean;
  onSelectBus?: (bus: number, additive: boolean, at?: { x: number; y: number }) => void;
  selectedBuses?: number[];
  onSelectLine?: (line: number, additive: boolean, at?: { x: number; y: number }) => void;
  selectedLines?: number[];
  onSelectTrafo?: (trafo: number, additive: boolean, at?: { x: number; y: number }) => void;
  selectedTrafos?: number[];
  batteryBuses?: number[];
  controllerBuses?: number[];   // 🎛 overload controllers (station = LV busbar)
  evBuses?: number[];          // runtime DER state (falls back to the topology)
  pvBuses?: number[];
  extFeedBuses?: number[];     // 📡 external nodes (live P/Q feed)
  // observability
  meterBuses?: number[];       // buses carrying a smart meter
  meterTrafos?: number[];      // transformers carrying a meter
  revealTruth?: boolean;       // overlay the true (unobserved) power flow, faded
}

interface Tip {
  x: number;
  y: number;
  lines: string[];
}

type XY = { x: number; y: number };

export default function GridDiagram({ topo, latest, showValues = false, onSelectBus, selectedBuses = [], onSelectLine, selectedLines = [], onSelectTrafo, selectedTrafos = [], batteryBuses = [], controllerBuses = [], evBuses, pvBuses, extFeedBuses = [], meterBuses = [], meterTrafos = [], revealTruth = false }: Props) {
  const { t } = useTranslation();
  const meteredBus = useMemo(() => new Set(meterBuses), [meterBuses]);
  const meteredTrafo = useMemo(() => new Set(meterTrafos), [meterTrafos]);
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
    // leaf loads fan both above and below their trunk node, so a track needs room
    // on each side: fanUp/fanDown = busiest half-fan above/below any node on it.
    const fanUp = new Map<number, number>();
    const fanDown = new Map<number, number>();
    // Track 0 is the main line. Its feeders alternate below (+) / above (−) so
    // cabinet connections use both sides; once a branch is off the spine it keeps
    // growing outward (never crossing back over the main line).
    let below = 0;
    let above = 0;
    const place = (n: number, t: number, dir: number) => {
      track.set(n, t);
      const ch = kids.get(n) ?? [];
      const internal = ch.filter((c) => !isLeaf(c)).sort((a, b) => (size.get(b)! - size.get(a)!) || (a - b));
      const leaves = ch.filter(isLeaf).sort((a, b) => a - b);
      leafKids.set(n, leaves);
      if (leaves.length) {
        fanDown.set(t, Math.max(fanDown.get(t) ?? 0, Math.ceil(leaves.length / 2)));
        fanUp.set(t, Math.max(fanUp.get(t) ?? 0, Math.floor(leaves.length / 2)));
      }
      internal.forEach((c, i) => {
        if (i === 0) { place(c, t, dir); return; } // trunk continues along this line
        if (dir > 0 || (dir === 0 && i % 2 === 1)) { below += 1; place(c, below, 1); }
        else { above -= 1; place(c, above, -1); }
      });
    };
    roots.forEach((r, idx) => {
      if (track.has(r)) return;
      if (idx === 0) place(r, 0, 0);
      else { below += 1; place(r, below, 1); } // extra components stack below
    });

    const used = [...track.values()];
    const minT = used.length ? Math.min(...used) : 0;
    const maxT = used.length ? Math.max(...used) : 0;
    const trackY = new Map<number, number>();
    let acc = PADY;
    for (let t = minT; t <= maxT; t++) {
      acc += (fanUp.get(t) ?? 0) * LEAF_STEP;                                     // room for the upward fan
      trackY.set(t, acc);
      acc += Math.max(TRACK_BASE, (fanDown.get(t) ?? 0) * LEAF_STEP) + TRACK_GAP; // downward fan + gap
    }
    const H = acc + PADY;
    const maxDepth = Math.max(1, ...depth.values());
    const W = PADX * 2 + maxDepth * COL;

    const pos = new Map<number, XY>();
    for (const [n, t] of track) pos.set(n, { x: PADX + (depth.get(n) ?? 0) * COL, y: trackY.get(t) ?? PADY });
    for (const [p, ls] of leafKids) {
      const pp = pos.get(p); if (!pp || !Number.isFinite(pp.y)) continue;
      const lx = PADX + ((depth.get(p) ?? 0) + 1) * COL;
      // fan below/above alternately: +1, -1, +2, -2, … (uses the space on both sides)
      ls.forEach((c, j) => {
        const off = (j % 2 === 0 ? 1 : -1) * (Math.floor(j / 2) + 1);
        pos.set(c, { x: lx, y: pp.y + off * LEAF_STEP });
      });
    }
    return { pos, parent, depthOf: depth, W, H };
  }, [topo]);

  // orthogonal edge: a short stub to the right of the parent, vertical riser
  // BETWEEN the node columns, then a horizontal tap to the child (a straight
  // horizontal line when they share a track). Keeping the riser off the column
  // prevents it from running through nodes of the tracks it crosses.
  const edge = (aId: number, bId: number): { p: XY; c: XY; d: string } | null => {
    const A = pos.get(aId); const B = pos.get(bId);
    if (!A || !B) return null;
    let p = A; let c = B;
    if (parent.get(aId) === bId) { p = B; c = A; }
    else if (parent.get(bId) === aId) { p = A; c = B; }
    else if ((depthOf.get(bId) ?? 0) < (depthOf.get(aId) ?? 0)) { p = B; c = A; }
    if (p.y === c.y) return { p, c, d: `M ${p.x} ${p.y} H ${c.x}` };
    const rx = p.x + (c.x >= p.x ? RISER_DX : -RISER_DX);
    return { p, c, d: `M ${p.x} ${p.y} H ${rx} V ${c.y} H ${c.x}` };
  };

  const [vb, setVb] = useState({ x: 0, y: 0, w: W, h: H });
  const [tip, setTip] = useState<Tip | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const drag = useRef<{ x: number; y: number } | null>(null);

  // re-frame to the whole diagram when the canvas size changes (grid swap)
  useEffect(() => setVb({ x: 0, y: 0, w: W, h: H }), [W, H]);

  // live lookups
  const liveLine = useMemo(() => {
    const m = new Map<number, NonNullable<StepResult["lines"]>[number]>();
    latest?.lines?.forEach((l) => m.set(l.index, l));
    return m;
  }, [latest]);
  const liveTrafo = useMemo(() => {
    const m = new Map<number, NonNullable<StepResult["trafos"]>[number]>();
    latest?.trafos?.forEach((t) => m.set(t.index, t));
    return m;
  }, [latest]);
  const liveBus = useMemo(() => {
    const m = new Map<number, number>();
    latest?.buses?.forEach((b) => m.set(b.index, b.vm_pu));
    return m;
  }, [latest]);
  const liveBusFull = useMemo(() => {
    const m = new Map<number, NonNullable<StepResult["buses"]>[number]>();
    latest?.buses?.forEach((b) => m.set(b.index, b));
    return m;
  }, [latest]);
  // observed readings (only at placed meters) — the default source of colour/values
  const measBus = useMemo(() => {
    const m = new Map<number, NodeMeasurement>();
    latest?.measurements?.nodes.forEach((n) => m.set(n.bus, n));
    return m;
  }, [latest]);
  const measTrafo = useMemo(() => {
    const m = new Map<number, TrafoMeasurement>();
    latest?.measurements?.trafos.forEach((tr) => m.set(tr.trafo, tr));
    return m;
  }, [latest]);

  const maxIka = useMemo(() => Math.max(1e-6, ...(latest?.lines?.map((l) => l.i_ka) ?? [0])), [latest]);
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
          const live = liveLine.get(ln.id);            // truth (absent in strict mode)
          const open = ln.in_service === false;
          // Lines carry no measurement device: unknown unless truth is revealed.
          const showTruth = revealTruth && !!live;
          const color = open ? "#888" : showTruth ? loadingColor(live!.loading_percent) : UNOBSERVED_LINE;
          const wdt = open ? 1.5 : showTruth ? currentWidth(live!.i_ka, maxIka) : 1.5;
          const rev = (live?.p_from_mw ?? 0) < 0;
          const sel = selectedLines.includes(ln.id);
          return (
            <g
              key={`l${ln.id}`}
              data-line={ln.id}
              style={{ cursor: "pointer" }}
              onClick={(e) => onSelectLine?.(ln.id, e.ctrlKey || e.metaKey, { x: e.clientX, y: e.clientY })}
              onContextMenu={(e) => { e.preventDefault(); onSelectLine?.(ln.id, false, { x: e.clientX, y: e.clientY }); }}
              onMouseEnter={(ev) =>
                showTip(ev, showTruth
                  ? [
                      t("tip.line", { name: ln.name ?? ln.id }),
                      t("tip.loadingPct", { v: fmt(live!.loading_percent, 1) }),
                      t("tip.currentA", { v: fmt(live!.i_ka * 1000, 1) }),
                      t("tip.powerKw", { v: fmt(live!.p_from_mw * 1000, 1) }),
                      t("tip.clickCurrent"),
                    ]
                  : [t("tip.line", { name: ln.name ?? ln.id }), t("tip.noMeter"), t("tip.clickCurrent")])
              }
            >
              {sel && <path d={e.d} fill="none" stroke="#ffd166" strokeWidth={wdt + 5} strokeLinejoin="round" strokeLinecap="round" opacity={0.5} />}
              {/* wide transparent hit area so thin feeders are easy to click */}
              <path d={e.d} fill="none" stroke="transparent" strokeWidth={Math.max(wdt, 10)} />
              <path
                d={e.d}
                fill="none"
                stroke={color}
                strokeWidth={open ? 1.5 : wdt}
                strokeLinejoin="round"
                strokeLinecap="round"
                strokeDasharray={open ? "5 7" : undefined}
                opacity={open ? 1 : showTruth ? 0.5 : 0.85}
                className={!open && showTruth && animate && Math.abs(live!.p_from_mw) > 1e-4 ? `flow${rev ? " rev" : ""}` : ""}
              />
            </g>
          );
        })}

        {/* transformers */}
        {topo.trafos.map((tr) => {
          const e = edge(tr.hv_bus, tr.lv_bus);
          if (!e) return null;
          const metered = meteredTrafo.has(tr.id);
          const meas = measTrafo.get(tr.id);            // observed (only if metered)
          const truth = liveTrafo.get(tr.id);           // reality (reveal / strict-off)
          // measured loading drives colour; else reveal-truth (faded); else unknown
          const loadingPct = metered ? meas?.loading_percent ?? null
                           : revealTruth ? truth?.loading_percent ?? null : null;
          const known = loadingPct != null;
          const color = known ? loadingColor(loadingPct) : UNOBSERVED;
          const faded = !metered && revealTruth;        // showing reality, not a reading
          const mx = (e.p.x + e.c.x) / 2;
          const my = e.c.y;
          const sel = selectedTrafos.includes(tr.id);
          return (
            <g
              key={`t${tr.id}`}
              data-trafo={tr.id}
              style={{ cursor: "pointer" }}
              onClick={(e) => onSelectTrafo?.(tr.id, e.ctrlKey || e.metaKey, { x: e.clientX, y: e.clientY })}
              onContextMenu={(e) => { e.preventDefault(); onSelectTrafo?.(tr.id, false, { x: e.clientX, y: e.clientY }); }}
              onMouseEnter={(ev) =>
                showTip(ev, [
                  t("tip.trafo", { name: tr.name ?? tr.id }),
                  t("tip.kva", { v: fmt(tr.sn_mva * 1000, 0) }),
                  metered ? t("tip.metered") : known ? "" : t("tip.noMeter"),
                  known ? t("tip.loadingPct", { v: fmt(loadingPct, 1) }) : "",
                  t("tip.clickPower"),
                ].filter(Boolean))
              }
            >
              <path d={e.d} fill="none" stroke={color} strokeWidth={6} strokeLinejoin="round" strokeLinecap="round"
                    opacity={faded ? 0.5 : 1} />
              <circle cx={mx} cy={my} r={sel ? 13 : 11} fill="#0b0d11"
                stroke={sel ? "#ffd166" : color} strokeWidth={sel ? 4 : 3}
                strokeDasharray={!known ? "3 3" : undefined} opacity={faded ? 0.6 : 1} />
              {metered && <MeterBadge x={mx - 15} y={my - 15} />}
              <text x={mx} y={my + 3} textAnchor="middle" fontSize="9" fill="#e6e6e6" opacity={faded ? 0.7 : 1}>
                {loadingPct != null ? Math.round(loadingPct) : "?"}
              </text>
            </g>
          );
        })}

        {/* buses — voltage revealed only where a smart meter is placed */}
        {topo.buses.map((bus) => {
          const p = pos.get(bus.id);
          if (!p) return null;
          const isExt = extBuses.has(bus.id);   // slack/substation: always the reference
          const metered = meteredBus.has(bus.id);
          const meas = measBus.get(bus.id);
          const truth = liveBus.get(bus.id);
          const vm = metered ? meas?.vm_pu ?? null
                   : revealTruth ? truth ?? null : null;
          const known = isExt || vm != null;
          const fill = isExt ? "#e6e6e6" : known ? voltageColor(vm) : UNOBSERVED;
          const faded = !metered && !isExt && revealTruth;
          const busSel = selectedBuses.includes(bus.id);
          return (
            <circle
              key={`b${bus.id}`}
              data-bus={bus.id}
              cx={p.x}
              cy={p.y}
              r={busSel ? (isExt ? 7 : 5) : isExt ? 6 : 3}
              fill={fill}
              fillOpacity={faded ? 0.55 : 1}
              stroke={busSel ? "#ffd166" : isExt ? "#7fd1ff" : "none"}
              strokeWidth={busSel ? 2.5 : isExt ? 2 : 0}
              style={{ cursor: "pointer" }}
              onClick={(e) => onSelectBus?.(bus.id, e.ctrlKey || e.metaKey, { x: e.clientX, y: e.clientY })}
              onContextMenu={(e) => { e.preventDefault(); onSelectBus?.(bus.id, false, { x: e.clientX, y: e.clientY }); }}
              onMouseEnter={(e) =>
                showTip(e, [
                  isExt ? t("tip.slack", { name: bus.name }) : t("tip.bus", { name: bus.name }),
                  t("tip.kv", { v: bus.vn_kv }),
                  metered ? t("tip.metered") : isExt ? "" : vm == null ? t("tip.noMeter") : "",
                  vm != null ? t("tip.vmPu", { v: fmt(vm * V_BASE, 1) }) : "",
                  metered && meas && meas.i_ka != null ? t("tip.meterI", { v: fmt(meas.i_ka * 1000, 1) }) : "",
                  t("tip.clickNode"),
                ].filter(Boolean))
              }
            />
          );
        })}

        {/* equipment icon row per bus: battery \u00B7 meter \u00B7 EV charging \u00B7 PV \u2014
            the same glyph language as the side-panel section badges */}
        {topo.buses.map((bus) => {
          const p = pos.get(bus.id);
          if (!p) return null;
          const tags = (batteryBuses.includes(bus.id) ? "\u{1F50B}" : "")
                     + (controllerBuses.includes(bus.id) ? "\u{1F39B}️" : "")
                     + (meteredBus.has(bus.id) ? "\u{1F4DF}" : "")
                     + (extFeedBuses.includes(bus.id) ? "\u{1F4E1}" : "")
                     + ((evBuses ?? topo.ev_buses ?? []).includes(bus.id) ? "\u{1F50C}" : "")
                     + ((pvBuses ?? topo.pv_buses ?? []).includes(bus.id) ? "\u2600\uFE0F" : "");
          if (!tags) return null;
          return (
            <text key={"eq" + bus.id} x={p.x + 5} y={p.y + 13} fontSize="8" pointerEvents="none">
              {tags}
            </text>
          );
        })}

        {/* SCADA value indicators. Measured readings at metered nodes; line
            currents + unmetered nodes appear only when ground truth is revealed. */}
        {showValues && latest && (
          <g pointerEvents="none">
            {revealTruth && topo.lines.map((ln) => {
              const e = edge(ln.from_bus, ln.to_bus);
              const live = liveLine.get(ln.id);
              if (!e || !live) return null;
              return (
                <ValueBox key={`vl${ln.id}`} x={(e.p.x + e.c.x) / 2} y={e.c.y}
                  rows={[`${fmt(live.i_ka * 1000, 0)} A`]} faded />
              );
            })}
            {topo.buses.map((bus) => {
              const p = pos.get(bus.id);
              if (!p) return null;
              const meas = measBus.get(bus.id);
              if (meteredBus.has(bus.id) && meas) {
                const rows = [meas.vm_pu != null ? `${fmt(meas.vm_pu * V_BASE, 0)} V` : "–"];
                if (meas.p_mw != null && Math.abs(meas.p_mw) > 1e-4) rows.push(`${fmt(meas.p_mw * 1000, 0)} kW`);
                return <ValueBox key={`vb${bus.id}`} x={p.x + 7} y={p.y} rows={rows} accent />;
              }
              const lb = revealTruth ? liveBusFull.get(bus.id) : undefined;
              if (!lb) return null;
              const rows = [`${fmt(lb.vm_pu * V_BASE, 0)} V`];
              if (Math.abs(lb.p_mw) > 1e-4) rows.push(`${fmt(lb.p_mw * 1000, 0)} kW`);
              return <ValueBox key={`vb${bus.id}`} x={p.x + 7} y={p.y} rows={rows} faded />;
            })}
          </g>
        )}
      </svg>

      <button className="ghost" style={{ position: "absolute", top: 10, right: 10 }} onClick={reset}>
        {t("live.reset")}
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
function ValueBox({ x, y, rows, accent, faded }: { x: number; y: number; rows: string[]; accent?: boolean; faded?: boolean }) {
  const w = Math.max(...rows.map((r) => r.length)) * 5.4 + 6;
  const h = rows.length * 10 + 3;
  return (
    <g transform={`translate(${x + 2},${y - h / 2})`} opacity={faded ? 0.6 : 1}>
      <rect width={w} height={h} rx={2} fill="#0b0d11" opacity={0.82} stroke="#33414f" strokeWidth={0.4} />
      {rows.map((r, i) => (
        <text key={i} x={3} y={9 + i * 10} fontSize={8.5}
          style={{ fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}
          fill={faded ? "#8a94a3" : accent ? "#8fe3c8" : "#74c0fc"}>
          {r}
        </text>
      ))}
    </g>
  );
}

// A small "meter" glyph (a filled tag) marking a node/transformer that carries a
// measurement device, so metered elements are identifiable at a glance.
function MeterBadge({ x, y }: { x: number; y: number }) {
  return (
    <g pointerEvents="none" transform={`translate(${x},${y})`}>
      <rect width={9} height={7} rx={1.5} fill="#4c8dff" stroke="#0b0d11" strokeWidth={0.6} />
      <circle cx={4.5} cy={3.5} r={1.4} fill="#0b0d11" />
    </g>
  );
}
