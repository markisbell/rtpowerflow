import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { StepResult, Topology } from "../types";
import { currentWidth, loadingColor, voltageColor } from "../scales";

interface Props {
  topo: Topology;
  latest: StepResult | null;
}

/** Live power-flow grid rendered on real OSM map tiles, using each bus's WGS84
 *  coordinates (ding0 grids). Lines/buses/transformers are Leaflet canvas vector
 *  layers, restyled in place on every WebSocket tick. */
export default function MapDiagram({ topo, latest }: Props) {
  const elRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const lineRef = useRef<Map<number, L.Polyline>>(new Map());
  const busRef = useRef<Map<number, L.CircleMarker>>(new Map());
  const trafoRef = useRef<Map<number, L.CircleMarker>>(new Map());

  // build map + static layers when the grid changes
  useEffect(() => {
    if (!elRef.current) return;
    const map = L.map(elRef.current, { preferCanvas: true, zoomSnap: 0.25 });
    mapRef.current = map;
    L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", {
      maxZoom: 20,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    }).addTo(map);

    const pos = new Map<number, [number, number]>(); // bus id -> [lat, lon]
    for (const b of topo.buses) if (b.geo) pos.set(b.id, [b.geo[1], b.geo[0]]);

    lineRef.current.clear();
    for (const ln of topo.lines) {
      const a = pos.get(ln.from_bus);
      const c = pos.get(ln.to_bus);
      if (!a || !c) continue;
      const pl = L.polyline([a, c], { color: "#64748b", weight: 2, opacity: 0.95 }).addTo(map);
      pl.bindTooltip(`Line ${ln.name ?? ln.id}`);
      lineRef.current.set(ln.id, pl);
    }

    const ext = new Set(topo.ext_grids.map((e) => e.bus));
    busRef.current.clear();
    for (const bus of topo.buses) {
      const p = pos.get(bus.id);
      if (!p) continue;
      const isExt = ext.has(bus.id);
      const cm = L.circleMarker(p, {
        radius: isExt ? 6 : 3,
        color: isExt ? "#7fd1ff" : "#94a3b8",
        weight: isExt ? 2 : 1,
        fillColor: isExt ? "#e6e6e6" : "#94a3b8",
        fillOpacity: 0.9,
      }).addTo(map);
      cm.bindTooltip(`${isExt ? "Slack " : "Bus "}${bus.name} · ${bus.vn_kv} kV`);
      busRef.current.set(bus.id, cm);
    }

    trafoRef.current.clear();
    for (const tr of topo.trafos) {
      const at = pos.get(tr.hv_bus) ?? pos.get(tr.lv_bus);
      if (!at) continue;
      const cm = L.circleMarker(at, {
        radius: 7,
        color: "#f59e0b",
        weight: 2,
        fillColor: "#0b0d11",
        fillOpacity: 1,
      }).addTo(map);
      cm.bindTooltip(`Trafo ${tr.name ?? tr.id} · ${(tr.sn_mva * 1000).toFixed(0)} kVA`);
      trafoRef.current.set(tr.id, cm);
    }

    const pts = [...pos.values()];
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.08));
    const t = setTimeout(() => map.invalidateSize(), 80);

    return () => {
      clearTimeout(t);
      map.remove();
      mapRef.current = null;
    };
  }, [topo]);

  // restyle from live results
  useEffect(() => {
    if (!latest || !mapRef.current) return;
    const maxIka = Math.max(1e-6, ...latest.lines.map((l) => l.i_ka));
    const lineLive = new Map(latest.lines.map((l) => [l.index, l]));
    for (const [id, pl] of lineRef.current) {
      const live = lineLive.get(id);
      pl.setStyle({ color: loadingColor(live?.loading_percent), weight: live ? currentWidth(live.i_ka, maxIka) : 1.5 });
    }
    const ext = new Set(topo.ext_grids.map((e) => e.bus));
    const vm = new Map(latest.buses.map((b) => [b.index, b.vm_pu]));
    for (const [id, cm] of busRef.current) {
      if (ext.has(id)) continue;
      const c = voltageColor(vm.get(id));
      cm.setStyle({ color: c, fillColor: c });
    }
    const trLive = new Map(latest.trafos.map((t) => [t.index, t]));
    for (const [id, cm] of trafoRef.current) {
      cm.setStyle({ color: loadingColor(trLive.get(id)?.loading_percent) });
    }
  }, [latest, topo]);

  return <div ref={elRef} className="map-canvas" />;
}
