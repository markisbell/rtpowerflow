import { useRef, useState } from "react";

export interface GSeries { label: string; color: string; data: (number | null)[]; fill?: boolean; }
export interface GLimit { value: number; label: string; color: string; }

const W = 300, H = 128, L = 30, R = 8, T = 10, B = 18;

// A daily time-series graph (x = 0..24 h) with overlaid semi-transparent series,
// dashed limit lines, and a cursor that follows the mouse and reads out each
// series' value at that time. `scale`/`unit`/`dec` format the readouts; series of
// different lengths are supported (each mapped across the day by its index).
export default function ProfileGraph({
  series, limits = [], scale, unit, dec, baseZero = true,
}: { series: GSeries[]; limits?: GLimit[]; scale: number; unit: string; dec: number; baseZero?: boolean }) {
  const [hover, setHover] = useState<number | null>(null); // fraction of the day 0..1
  const ref = useRef<SVGSVGElement | null>(null);

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
  const line = (s: GSeries) => {
    let d = "";
    s.data.forEach((v, i) => {
      if (v == null) return;
      d += (d ? "L" : "M") + px(s.data.length <= 1 ? 0 : i / (s.data.length - 1)).toFixed(1) + " " + py(v).toFixed(1) + " ";
    });
    return d;
  };
  const valAt = (s: GSeries, frac: number) => s.data[Math.round(frac * (s.data.length - 1))] ?? null;

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
      <div style={{ fontSize: "0.72rem", color: "var(--muted)", height: 13 }}>{hover != null ? hhmm(hover) : ""}</div>
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
                       fill={s.color} opacity={0.14} />;
        })}
        {series.map((s) => <path key={"l" + s.label} d={line(s)} fill="none" stroke={s.color} strokeWidth={1.5} opacity={0.92} />)}

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
