// Shared color/size scales for the power-flow visualization.

/** Color for an element with no measurement — deliberately dim/neutral so the
 *  eye reads it as "unknown", not as a valid low/green reading. */
export const UNOBSERVED = "#39424f";
export const UNOBSERVED_LINE = "#2b323c";

/** Loading % -> traffic-light color (green < 50 < amber < 80 < orange < 100 < red). */
export function loadingColor(pct: number | null | undefined): string {
  if (pct == null) return "#64748b";
  if (pct < 50) return "#22c55e";
  if (pct < 80) return "#f59e0b";
  if (pct < 100) return "#f97316";
  return "#ef4444";
}

/** Bus voltage [pu] -> color. In-band gray, under-voltage blue, over-voltage red. */
export function voltageColor(vm: number | null | undefined): string {
  if (vm == null) return "#64748b";
  if (vm < 0.95) return "#3b82f6";
  if (vm > 1.05) return "#ef4444";
  if (vm < 0.97 || vm > 1.03) return "#f59e0b";
  return "#94a3b8";
}

/** Map a current to a stroke width, relative to the snapshot's max current. */
export function currentWidth(i_ka: number, maxIka: number): number {
  if (maxIka <= 0) return 1.5;
  return 1 + 5 * Math.sqrt(Math.max(i_ka, 0) / maxIka);
}

/** Display base for voltages: 1.0 pu is shown as 230 V (German LV phase
 *  voltage), so the EN 50160 band reads as the familiar 207...253 V. */
export const V_BASE = 230;

export function fmt(n: number | null | undefined, digits = 2): string {
  if (n == null) return "–";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

// ---- continuous colormaps for the map view ----------------------------------
// Lines are colored green → amber → red by loading % (a continuous version of
// the schematic's traffic-light scale — deliberately no blue, so an idle line
// still reads as "healthy", not "cold"). Nodes use matplotlib 'Reds' by voltage
// deviation, as in ding0's plot_mv_topology.

type Stop = [number, [number, number, number]];

const LOADING: Stop[] = [
  [0.0, [34, 197, 94]],     // idle .... green  (#22c55e)
  [0.5, [245, 158, 11]],    // half .... amber  (#f59e0b)
  [0.75, [249, 115, 22]],   // high .... orange (#f97316)
  [1.0, [239, 68, 68]],     // full .... red    (#ef4444)
];

const REDS: Stop[] = [
  [0.0, [255, 245, 240]],
  [0.25, [252, 187, 161]],
  [0.5, [251, 106, 74]],
  [0.75, [203, 24, 29]],
  [1.0, [103, 0, 13]],
];

function ramp(stops: Stop[], t: number): string {
  const x = Math.min(1, Math.max(0, t));
  for (let i = 1; i < stops.length; i++) {
    if (x <= stops[i][0]) {
      const [t0, c0] = stops[i - 1];
      const [t1, c1] = stops[i];
      const f = t1 === t0 ? 0 : (x - t0) / (t1 - t0);
      const c = c0.map((v, k) => Math.round(v + (c1[k] - v) * f));
      return `rgb(${c[0]},${c[1]},${c[2]})`;
    }
  }
  const last = stops[stops.length - 1][1];
  return `rgb(${last[0]},${last[1]},${last[2]})`;
}

/** Line loading % -> green (idle) … amber … red (overload). */
export function lineLoadingColor(pct: number | null | undefined): string {
  if (pct == null) return "rgb(120,120,120)";
  return ramp(LOADING, pct / 100);
}

/** Bus voltage [pu] -> 'Reds' by deviation from 1.0 (white=nominal, red=stressed). */
export function voltageReds(vm: number | null | undefined): string {
  if (vm == null) return "rgb(200,200,200)";
  return ramp(REDS, Math.abs(vm - 1.0) / 0.06);
}

/** CSS linear-gradient string for a colorbar legend of the given colormap. */
function gradientCss(stops: Stop[]): string {
  const parts = stops.map(([t, c]) => `rgb(${c[0]},${c[1]},${c[2]}) ${(t * 100).toFixed(0)}%`);
  return `linear-gradient(to top, ${parts.join(", ")})`;
}

export const LOADING_GRADIENT = gradientCss(LOADING);
export const REDS_GRADIENT = gradientCss(REDS);
