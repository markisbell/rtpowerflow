// Shared color/size scales for the power-flow visualization.

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

export function fmt(n: number | null | undefined, digits = 2): string {
  if (n == null) return "–";
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}
