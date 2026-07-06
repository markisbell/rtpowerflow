import { useMemo } from "react";
import type { GridPreview } from "../types";

/** Bare structure of a grid on a dark canvas — no map tiles, no live values.
 *  Real lon/lat positions, cables along their street geometry; the station is
 *  amber, cabinets green squares, customers green dots, junctions grey. */
export default function StructureDiagram({ p, height = 250 }: {
  p: GridPreview; height?: number;
}) {
  const W = 560;
  const model = useMemo(() => {
    const geo = new Map<number, [number, number]>();
    for (const b of p.buses) if (b.geo) geo.set(b.id, b.geo as [number, number]);
    if (geo.size < 2) return null;

    const lons = [...geo.values()].map((g) => g[0]);
    const lats = [...geo.values()].map((g) => g[1]);
    const minLon = Math.min(...lons), maxLat = Math.max(...lats);
    const midLat = (Math.min(...lats) + maxLat) / 2;
    const cos = Math.cos((midLat * Math.PI) / 180);
    const px = (lon: number) => (lon - minLon) * cos;
    const py = (lat: number) => maxLat - lat;
    const w0 = Math.max(...lons.map(px), 1e-9);
    const h0 = Math.max(...lats.map(py), 1e-9);
    const pad = 14;
    const s = Math.min((W - 2 * pad) / w0, (height - 2 * pad) / h0);
    const ox = (W - w0 * s) / 2;
    const oy = (height - h0 * s) / 2;
    const P = (g: [number, number]): [number, number] =>
      [ox + px(g[0]) * s, oy + py(g[1]) * s];

    const polylines: string[] = [];
    for (const ln of p.lines) {
      const pts = ln.geometry && ln.geometry.length >= 2
        ? ln.geometry
        : (geo.has(ln.from_bus) && geo.has(ln.to_bus)
          ? [geo.get(ln.from_bus)!, geo.get(ln.to_bus)!] : null);
      if (pts) polylines.push(pts.map((g) => P(g as [number, number]).map((v) => v.toFixed(1)).join(",")).join(" "));
    }
    const station = new Set<number>([...(p.slack_buses ?? []),
                                     ...p.trafos.map((t) => t.lv_bus)]);
    const loads = new Set<number>(p.load_buses ?? []);
    const nodes = p.buses.filter((b) => b.geo).map((b) => ({
      id: b.id, at: P(b.geo as [number, number]),
      cls: station.has(b.id) ? "station"
        : b.kind === "cabinet" ? "cabinet"
        : loads.has(b.id) ? "load" : "junction",
    }));
    return { polylines, nodes };
  }, [p, height]);

  if (!model) return null;
  return (
    <svg viewBox={`0 0 ${W} ${height}`} style={{ width: "100%", height: "auto",
         background: "#05070c", borderRadius: 8, border: "1px solid var(--border)" }}>
      {model.polylines.map((pts, i) => (
        <polyline key={i} points={pts} fill="none" stroke="#4c8dff"
                  strokeWidth={1.3} strokeOpacity={0.85} strokeLinecap="round" />
      ))}
      {model.nodes.map((n) =>
        n.cls === "station" ? (
          <rect key={n.id} x={n.at[0] - 4} y={n.at[1] - 4} width={8} height={8}
                fill="#f2ae00" rx={1.5} />
        ) : n.cls === "cabinet" ? (
          <rect key={n.id} x={n.at[0] - 2.6} y={n.at[1] - 2.6} width={5.2} height={5.2}
                fill="#3fb950" rx={1} />
        ) : n.cls === "load" ? (
          <circle key={n.id} cx={n.at[0]} cy={n.at[1]} r={2.4} fill="#3fb950" />
        ) : (
          <circle key={n.id} cx={n.at[0]} cy={n.at[1]} r={1.4} fill="#5a6472" />
        ),
      )}
    </svg>
  );
}
