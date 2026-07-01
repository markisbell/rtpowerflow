import { useEffect, useRef, useState } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { StepResult, Topology } from "../types";
import { currentWidth, jetColor, voltageReds, JET_GRADIENT, REDS_GRADIENT } from "../scales";

interface Props {
  topo: Topology;
  latest: StepResult | null;
  onSelectBus?: (bus: number, additive: boolean) => void;
  onSelectLine?: (line: number, additive: boolean) => void;
  onSelectTrafo?: (trafo: number, additive: boolean) => void;
  batteryBuses?: number[];
}

const isAdditive = (e: L.LeafletMouseEvent) =>
  !!(e.originalEvent && (e.originalEvent.ctrlKey || e.originalEvent.metaKey));

const STATION_COLOR = "#f2ae00"; // ding0 MVStation amber

const TILES = {
  light: {
    url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    bg: "#e9eaec",
    stroke: "#3a3a3a", // node outline on light bg
  },
  dark: {
    url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    bg: "#0b0d11",
    stroke: "#0b0d11",
  },
};

/** Live power-flow grid on real OSM tiles at each bus's WGS84 coordinate (ding0
 *  grids). Styled to mimic ding0's plot_mv_topology: light basemap, lines on a
 *  jet colormap by loading, nodes on a Reds ramp by voltage, amber MV station.
 *  All vector layers are Leaflet canvas markers restyled in place every tick. */
export default function MapDiagram({ topo, latest, onSelectBus, onSelectLine, onSelectTrafo, batteryBuses = [] }: Props) {
  const onSelectRef = useRef(onSelectBus);
  onSelectRef.current = onSelectBus;
  const onSelectLineRef = useRef(onSelectLine);
  onSelectLineRef.current = onSelectLine;
  const onSelectTrafoRef = useRef(onSelectTrafo);
  onSelectTrafoRef.current = onSelectTrafo;
  const elRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<L.Map | null>(null);
  const tileRef = useRef<L.TileLayer | null>(null);
  const lineRef = useRef<Map<number, L.Polyline>>(new Map());
  const busRef = useRef<Map<number, L.CircleMarker>>(new Map());
  const trafoRef = useRef<Map<number, L.CircleMarker>>(new Map());
  const batteryRef = useRef<L.CircleMarker[]>([]);
  const [light, setLight] = useState(true);
  const batKey = batteryBuses.join(",");

  // build map + static layers when the grid changes
  useEffect(() => {
    if (!elRef.current) return;
    const map = L.map(elRef.current, { preferCanvas: true, zoomSnap: 0.25, attributionControl: false });
    mapRef.current = map;
    map.zoomControl.setPosition("bottomleft");   // move +/- to the lower left
    L.control.attribution({ prefix: false, position: "bottomleft" }).addAttribution(
      '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    ).addTo(map);

    const stroke = (light ? TILES.light : TILES.dark).stroke;
    const pos = new Map<number, [number, number]>(); // bus id -> [lat, lon]
    for (const b of topo.buses) if (b.geo) pos.set(b.id, [b.geo[1], b.geo[0]]);

    lineRef.current.clear();
    for (const ln of topo.lines) {
      // OSM-routed cables carry a [lon,lat] polyline that follows the streets;
      // otherwise draw a straight segment between the two bus coordinates.
      let latlngs: L.LatLngExpression[];
      if (ln.geometry && ln.geometry.length >= 2) {
        latlngs = ln.geometry.map(([lon, lat]) => [lat, lon] as [number, number]);
      } else {
        const a = pos.get(ln.from_bus);
        const c = pos.get(ln.to_bus);
        if (!a || !c) continue;
        latlngs = [a, c];
      }
      const open = ln.in_service === false; // normally-open ring tie (suburban)
      const pl = L.polyline(latlngs, open
        ? { color: "#888", weight: 2, opacity: 0.85, dashArray: "5 7" }
        : { color: jetColor(null), weight: 2, opacity: 0.95 }).addTo(map);
      pl.bindTooltip(open ? `Line ${ln.name ?? ln.id} · normally open` : `Line ${ln.name ?? ln.id} · click for graph`);
      pl.on("click", (e) => onSelectLineRef.current?.(ln.id, isAdditive(e)));
      lineRef.current.set(ln.id, pl);
    }

    const ext = new Set(topo.ext_grids.map((e) => e.bus));
    const cabs = new Set(topo.cabinet_buses ?? []);
    busRef.current.clear();
    for (const bus of topo.buses) {
      const p = pos.get(bus.id);
      if (!p) continue;
      const isExt = ext.has(bus.id);
      const isCab = cabs.has(bus.id);  // LV cable cabinet → green circle
      const cm = L.circleMarker(p, {
        radius: isExt ? 7 : isCab ? 5 : 3,
        color: isExt ? "#7a5400" : isCab ? "#1a7a1a" : stroke,
        weight: isExt ? 1.5 : isCab ? 2 : 0.5,
        fillColor: isExt ? STATION_COLOR : isCab ? "#eafbe7" : "rgb(200,200,200)",
        fillOpacity: isExt ? 1 : isCab ? 1 : 0.9,
      }).addTo(map);
      cm.bindTooltip(`${isExt ? "MV station " : isCab ? "Cable cabinet " : "Bus "}${bus.name} · ${bus.vn_kv} kV · click for graph`);
      cm.on("click", (e) => onSelectRef.current?.(bus.id, isAdditive(e)));
      busRef.current.set(bus.id, cm);
    }

    trafoRef.current.clear();
    for (const tr of topo.trafos) {
      const at = pos.get(tr.lv_bus) ?? pos.get(tr.hv_bus);
      if (!at) continue;
      const cm = L.circleMarker(at, {
        radius: 4,
        color: "#7a5400",
        weight: 1,
        fillColor: STATION_COLOR,
        fillOpacity: 0.95,
      }).addTo(map);
      cm.bindTooltip(`Trafo ${tr.name ?? tr.id} · ${(tr.sn_mva * 1000).toFixed(0)} kVA · click for graph`);
      cm.on("click", (e) => onSelectTrafoRef.current?.(tr.id, isAdditive(e)));
      trafoRef.current.set(tr.id, cm);
    }

    const pts = [...pos.values()];
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.08));
    const t = setTimeout(() => map.invalidateSize(), 80);

    return () => {
      clearTimeout(t);
      map.remove();
      mapRef.current = null;
      tileRef.current = null;
    };
  }, [topo]);

  // (re)apply the basemap when the light/dark choice or grid changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const theme = light ? TILES.light : TILES.dark;
    if (tileRef.current) map.removeLayer(tileRef.current);
    tileRef.current = L.tileLayer(theme.url, { maxZoom: 20 }).addTo(map);
    tileRef.current.bringToBack();
    map.getContainer().style.background = theme.bg;
    // node outlines need to flip with the background for contrast (keep cabinets green)
    const ext = new Set(topo.ext_grids.map((e) => e.bus));
    const cabs = new Set(topo.cabinet_buses ?? []);
    for (const [id, cm] of busRef.current) {
      if (!ext.has(id) && !cabs.has(id)) cm.setStyle({ color: theme.stroke });
    }
  }, [light, topo]);

  // restyle from live results (jet by loading, Reds by voltage)
  useEffect(() => {
    if (!latest || !mapRef.current) return;
    const maxIka = Math.max(1e-6, ...latest.lines.map((l) => l.i_ka));
    const lineLive = new Map(latest.lines.map((l) => [l.index, l]));
    const openLines = new Set(topo.lines.filter((l) => l.in_service === false).map((l) => l.id));
    for (const [id, pl] of lineRef.current) {
      if (openLines.has(id)) continue; // keep normally-open ties dashed grey
      const live = lineLive.get(id);
      pl.setStyle({ color: jetColor(live?.loading_percent), weight: live ? currentWidth(live.i_ka, maxIka) : 1.5 });
    }
    const ext = new Set(topo.ext_grids.map((e) => e.bus));
    const cabs = new Set(topo.cabinet_buses ?? []);
    const vm = new Map(latest.buses.map((b) => [b.index, b.vm_pu]));
    for (const [id, cm] of busRef.current) {
      if (ext.has(id) || cabs.has(id)) continue;  // cabinets stay green
      cm.setStyle({ fillColor: voltageReds(vm.get(id)) });
    }
  }, [latest, topo]);

  // battery markers (green ring) at battery buses; redraw when the set changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    batteryRef.current.forEach((m) => map.removeLayer(m));
    batteryRef.current = [];
    const set = new Set(batteryBuses);
    for (const b of topo.buses) {
      if (!set.has(b.id) || !b.geo) continue;
      const m = L.circleMarker([b.geo[1], b.geo[0]], {
        radius: 6, color: "#0b0d11", weight: 1.5, fillColor: "#3fb950", fillOpacity: 1,
      }).addTo(map);
      m.bindTooltip(`Battery · bus ${b.id}`);
      batteryRef.current.push(m);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topo, batKey]);

  return (
    <div className="map-wrap">
      <div ref={elRef} className="map-canvas" />
      <button className="map-basemap" onClick={() => setLight((v) => !v)} title="Toggle basemap">
        {light ? "🌙 Dark" : "☀ Light"}
      </button>
      <div className="map-colorbars">
        <Colorbar gradient={JET_GRADIENT} top="100%" bottom="0%" caption="line loading" />
        <Colorbar gradient={REDS_GRADIENT} top="±6%" bottom="0%" caption="bus voltage Δ" />
      </div>
    </div>
  );
}

function Colorbar({ gradient, top, bottom, caption }: { gradient: string; top: string; bottom: string; caption: string }) {
  return (
    <div className="colorbar">
      <span className="cb-top">{top}</span>
      <div className="cb-ramp" style={{ background: gradient }} />
      <span className="cb-bot">{bottom}</span>
      <span className="cb-cap">{caption}</span>
    </div>
  );
}
