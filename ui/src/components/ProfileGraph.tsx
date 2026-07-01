import { useRef, useState } from "react";

export interface GSeries {
  label: string;
  color: string;
  data: (number | null)[];
  fill?: boolean;
  // optional severity coloring for the "past" part of the curve (up to now), so
  // it matches the map/schematic colorset: colorFn maps colorData[i] → a color.
  colorData?: (number | null)[];
  colorFn?: (v: number | null | undefined) => string;
}
export interface GLimit { value: number; label: string; color: string; }

const W = 300, H = 128, L = 30, R = 8, T = 10, B = 18;

// A daily time-series graph (x = 0..24 h) with overlaid series, dashed limit
// lines, and a cursor that reads out each series at the hovered time. If `now`
// (fraction of the day) is given, the curve up to the current simulation time is
// drawn opaque — colored by the severity colorset when provided — and the rest
// faded, so the reading ties back to the live line/node color on the map.
export default function ProfileGraph({
  series, limits = [], scale, unit, dec, baseZero = true, now = null,
}: { series: GSeries[]; limits?: GLimit[]; scale: number; unit: string; dec: number; baseZero?: boolean; now?: number | null }) {
  const [hover, setHover] = useState<number | null>(null); // fraction of the day 0..1
  const ref = useRef<SVGSVGElement | null>(null);
  const nowF = now == null ? null : Math.min(1, Math.max(0, now));

  const vals: number[] = [];
  series.forEach((s) => s.data.forEach((v) => v != null && vals.push(v)));
  limits.forEach((l) => vals.push(l.value));
  let yMax = vals.length ? Math.max(...vals) : 1;
  let yMin = baseZero ? 0 : vals.length ? Math.min(...vals) : 0;
  const pad = (yMax - yMin) * 0.08 || Math.abs(yMax) * 0.08 || 0.01;
  yMax += pad;
  if (!baseZero) yMin -= pad;
  if (yMax <= yMin) yMax = yMin + 1;

  const px = (frac: number) => L + frac * (W - L - R);
  const py = (v: number) => H - B - ((v - yMin) / (yMax - yMin)) * (H - T - B);
  const fracOf = (s: GSeries, i: number) => (s.data.length <= 1 ? 0 : i / (s.data.length - 1));
  // step-after staircase (each sample's value held until the next) — no linear
  // interpolation between samples.
  const stepPath = (pts: [number, number][]) => {
    if (!pts.length) return "";
    let d = `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
    for (let k = 1; k < pts.length; k++) d += ` H ${pts[k][0].toFixed(1)} V ${pts[k][1].toFixed(1)}`;
    return d;
  };
  const pointsUpTo = (s: GSeries, upto: number): [number, number][] => {
    const out: [number, number][] = [];
    s.data.forEach((v, i) => {
      if (v != null && fracOf(s, i) <= upto + 1e-9) out.push([px(fracOf(s, i)), py(v)]);
    });
    return out;
  };
  const line = (s: GSeries) => stepPath(pointsUpTo(s, 1));
  // the "past" curve (frac <= now) as a single step path (uncolored series)
  const pastPath = (s: GSeries, nf: number) => stepPath(pointsUpTo(s, nf));
  // the "past" curve as per-segment colored step paths (severity colorset)
  const pastSegs = (s: GSeries, nf: number) => {
    const segs: { d: string; col: string }[] = [];
    for (let i = 0; i < s.data.length - 1; i++) {
      const a = s.data[i], b = s.data[i + 1];
      if (a == null || b == null) continue;
      const f1 = fracOf(s, i), f2 = fracOf(s, i + 1);
      if ((f1 + f2) / 2 > nf) continue;
      const x1 = px(f1), y1 = py(a), x2 = px(f2), y2 = py(b);
      const col = s.colorFn ? s.colorFn(s.colorData ? s.colorData[i] : a) : s.color;
      segs.push({ d: `M ${x1.toFixed(1)} ${y1.toFixed(1)} H ${x2.toFixed(1)} V ${y2.toFixed(1)}`, col });
    }
    return segs;
  };
  // step-after: the value shown at a position is the sample whose plateau it's on
  const valAt = (s: GSeries, frac: number) =>
    s.data[Math.min(s.data.length - 1, Math.floor(frac * (s.data.length - 1)))] ?? null;

  const onMove = (e: React.MouseEvent) => {
    const r = ref.current!.getBoundingClientRect();
    const f = (((e.clientX - r.left) / r.width) * W - L) / (W - L - R);
    setHover(f < 0 || f > 1 ? null : f);
  };
  const hhmm = (frac: number) => {
    const m = Math.round(frac * 1440);
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  };
  const fmtV = (v: number) => `${(v * scale).toFixed(dec)} ${unit}`;

  return (
    <>
      <div style={{ fontSize: "0.72rem", color: "var(--muted)", height: 13 }}>
        {hover != null ? hhmm(hover) : nowF != null ? `${hhmm(nowF)} Uhr` : ""}
      </div>
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} width="100%" style={{ display: "block", cursor: "crosshair" }}
           onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        {[0, 6, 12, 18, 24].map((h) => {
          const x = px(h / 24);
          return (
            <g key={h}>
              <line x1={x} y1={T} x2={x} y2={H - B} stroke="#1b2028" strokeWidth={0.5} />
              <text x={x} y={H - 6} fontSize={7} textAnchor="middle" fill="#6b7480">{h}</text>
            </g>
          );
        })}
        <line x1={L} y1={H - B} x2={W - R} y2={H - B} stroke="#2b3340" strokeWidth={0.8} />
        {/* zero reference line for curves that cross 0 (e.g. battery charge/discharge) */}
        {yMin < 0 && yMax > 0 && (
          <>
            <line x1={L} y1={py(0)} x2={W - R} y2={py(0)} stroke="#4a5568" strokeWidth={0.8} />
            <text x={2} y={py(0) + 2.5} fontSize={7} fill="#6b7480">0</text>
          </>
        )}
        <text x={2} y={T + 5} fontSize={7} fill="#6b7480">{(yMax * scale).toFixed(dec)}</text>
        <text x={2} y={H - B} fontSize={7} fill="#6b7480">{(yMin * scale).toFixed(dec)}</text>

        {limits.map((lm, i) => {
          const y = py(lm.value);
          return (
            <g key={"lim" + i}>
              <line x1={L} y1={y} x2={W - R} y2={y} stroke={lm.color} strokeWidth={0.8} strokeDasharray="4 3" opacity={0.85} />
              <text x={W - R} y={y - 2} fontSize={6.5} textAnchor="end" fill={lm.color}>{lm.label}</text>
            </g>
          );
        })}

        {series.map((s) => {
          const d = line(s);
          if (!s.fill || !d) return null;
          return <path key={"a" + s.label} d={`${d}L${px(1).toFixed(1)} ${py(yMin).toFixed(1)} L${px(0).toFixed(1)} ${py(yMin).toFixed(1)} Z`}
                       fill={s.color} opacity={nowF != null ? 0.09 : 0.14} />;
        })}
        {series.map((s) => {
          const full = line(s);
          if (!full) return null;
          return (
            <g key={"l" + s.label}>
              {/* whole day, faded when a "now" split is active (this is the future part) */}
              <path d={full} fill="none" stroke={s.color} strokeWidth={1.5} opacity={nowF != null ? 0.25 : 0.92} />
              {/* opaque past overlay up to the current time, severity-colored if provided */}
              {nowF != null && (s.colorFn
                ? pastSegs(s, nowF).map((sg, k) => (
                    <path key={k} d={sg.d} fill="none" stroke={sg.col} strokeWidth={2} strokeLinejoin="round" />
                  ))
                : <path d={pastPath(s, nowF)} fill="none" stroke={s.color} strokeWidth={2} />)}
            </g>
          );
        })}

        {/* current simulation time marker */}
        {nowF != null && (
          <line x1={px(nowF)} y1={T} x2={px(nowF)} y2={H - B} stroke="#e6e6e6" strokeWidth={1} opacity={0.5} />
        )}

        {hover != null && (
          <g pointerEvents="none">
            <line x1={px(hover)} y1={T} x2={px(hover)} y2={H - B} stroke="#9aa4b0" strokeWidth={0.8} strokeDasharray="3 3" />
            {series.map((s) => {
              const v = valAt(s, hover);
              return v == null ? null : <circle key={s.label} cx={px(hover)} cy={py(v)} r={2.6} fill={s.color} stroke="#0b0d11" strokeWidth={0.7} />;
            })}
          </g>
        )}
      </svg>

      <div style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem 0.8rem", fontSize: "0.72rem", marginTop: 2 }}>
        {series.map((s) => {
          const v = hover != null ? valAt(s, hover) : null;
          return (
            <span key={s.label} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <i style={{ width: 9, height: 9, borderRadius: 2, background: s.color, display: "inline-block" }} />
              {s.label}
              {v != null && <b style={{ fontVariantNumeric: "tabular-nums" }}>{fmtV(v)}</b>}
            </span>
          );
        })}
      </div>
    </>
  );
}
