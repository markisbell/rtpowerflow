import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { NodeProfiles, NodeSeriesKind } from "../types";

const COLOR: Record<NodeSeriesKind, string> = {
  residential: "#4c8dff",
  ev: "#f2ae00",
  pv: "#3fb950",
};
const LABEL: Record<NodeSeriesKind, string> = {
  residential: "Residential",
  ev: "EV",
  pv: "PV",
};

const W = 300, H = 132, L = 30, R = 8, T = 8, B = 18;

// A per-node daily load/generation graph: residential, EV and PV curves overlaid
// semi-transparently, with a cursor that follows the mouse and reads out each
// curve's power at that time.
export default function NodeProfile({ bus, name, onClose }: { bus: number; name: string; onClose: () => void }) {
  const [data, setData] = useState<NodeProfiles | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [hover, setHover] = useState<number | null>(null); // step under the cursor
  const svgRef = useRef<SVGSVGElement | null>(null);

  useEffect(() => {
    let alive = true;
    setData(null); setErr(null); setHover(null);
    api.nodeProfiles(bus).then((d) => alive && setData(d)).catch((e) => alive && setErr(String(e)));
    return () => { alive = false; };
  }, [bus]);

  const steps = data?.steps_per_day ?? 1440;
  const series = data?.series ?? [];
  const maxP = useMemo(() => {
    let m = 1e-9;
    for (const s of series) for (const v of s.p_mw) if (v != null && v > m) m = v;
    return m;
  }, [series]);

  const px = (step: number) => L + (step / (steps - 1)) * (W - L - R);
  const py = (mw: number) => H - B - (mw / maxP) * (H - T - B);

  const paths = useMemo(
    () =>
      series.map((s) => {
        let line = "";
        s.p_mw.forEach((v, i) => {
          if (v == null) return;
          line += (line ? "L" : "M") + px(i).toFixed(1) + " " + py(v).toFixed(1) + " ";
        });
        const area = line
          ? line + `L${px(steps - 1).toFixed(1)} ${py(0).toFixed(1)} L${px(0).toFixed(1)} ${py(0).toFixed(1)} Z`
          : "";
        return { kind: s.kind, line, area };
      }),
    [series, maxP, steps],
  );

  const onMove = (e: React.MouseEvent) => {
    const r = svgRef.current!.getBoundingClientRect();
    const frac = (((e.clientX - r.left) / r.width) * W - L) / (W - L - R);
    setHover(frac < 0 || frac > 1 ? null : Math.round(frac * (steps - 1)));
  };
  const hhmm = (step: number) => {
    const mins = Math.round((step / steps) * 1440);
    return `${String(Math.floor(mins / 60)).padStart(2, "0")}:${String(mins % 60).padStart(2, "0")}`;
  };

  return (
    <div style={{ marginTop: "0.7rem", borderTop: "1px solid var(--border)", paddingTop: "0.5rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", fontSize: "0.78rem" }}>
        <span style={{ fontWeight: 600 }}>
          Node {name}
          {hover != null && <span className="muted" style={{ fontWeight: 400 }}> · {hhmm(hover)}</span>}
        </span>
        <button className="ghost" style={{ fontSize: "0.7rem", padding: "0 6px" }} onClick={onClose}>✕</button>
      </div>

      {err && <div className="muted" style={{ fontSize: "0.72rem" }}>error: {err}</div>}
      {!err && !data && <div className="muted" style={{ fontSize: "0.72rem" }}>loading…</div>}
      {!err && data && series.length === 0 && (
        <div className="muted" style={{ fontSize: "0.72rem" }}>No load or generation at this node.</div>
      )}

      {series.length > 0 && (
        <>
          <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block", cursor: "crosshair" }}
               onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
            {[0, 6, 12, 18, 24].map((h) => {
              const x = px((h / 24) * (steps - 1));
              return (
                <g key={h}>
                  <line x1={x} y1={T} x2={x} y2={H - B} stroke="#1b2028" strokeWidth={0.5} />
                  <text x={x} y={H - 6} fontSize={7} textAnchor="middle" fill="#6b7480">{h}</text>
                </g>
              );
            })}
            <line x1={L} y1={H - B} x2={W - R} y2={H - B} stroke="#2b3340" strokeWidth={0.8} />
            <text x={2} y={T + 6} fontSize={7} fill="#6b7480">{(maxP * 1000).toFixed(0)} kW</text>

            {paths.map((p) => <path key={"a" + p.kind} d={p.area} fill={COLOR[p.kind]} opacity={0.16} />)}
            {paths.map((p) => <path key={"l" + p.kind} d={p.line} fill="none" stroke={COLOR[p.kind]} strokeWidth={1.4} opacity={0.9} />)}

            {hover != null && (
              <g pointerEvents="none">
                <line x1={px(hover)} y1={T} x2={px(hover)} y2={H - B} stroke="#9aa4b0" strokeWidth={0.8} strokeDasharray="3 3" />
                {series.map((s) => {
                  const v = s.p_mw[hover];
                  return v == null ? null : (
                    <circle key={s.kind} cx={px(hover)} cy={py(v)} r={2.6} fill={COLOR[s.kind]} stroke="#0b0d11" strokeWidth={0.7} />
                  );
                })}
              </g>
            )}
          </svg>

          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.4rem 0.8rem", fontSize: "0.72rem", marginTop: 2 }}>
            {series.map((s) => {
              const v = hover != null ? s.p_mw[hover] : null;
              return (
                <span key={s.kind} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                  <i style={{ width: 9, height: 9, borderRadius: 2, background: COLOR[s.kind], display: "inline-block" }} />
                  {LABEL[s.kind]}
                  {v != null && <b style={{ fontVariantNumeric: "tabular-nums" }}>{(v * 1000).toFixed(1)} kW</b>}
                </span>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
