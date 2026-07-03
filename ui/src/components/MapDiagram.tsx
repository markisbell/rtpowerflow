import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { StepResult, Topology } from "../types";
import { currentWidth, lineLoadingColor, voltageReds, LOADING_GRADIENT, REDS_GRADIENT, UNOBSERVED, UNOBSERVED_LINE } from "../scales";

interface Props {
  topo: Topology;
  latest: StepResult | null;
  onSelectBus?: (bus: number, additive: boolean, at?: { x: number; y: number }) => void;
  onSelectLine?: (line: number, additive: boolean, at?: { x: number; y: number }) => void;
  onSelectTrafo?: (trafo: number, additive: boolean, at?: { x: number; y: number }) => void;
  batteryBuses?: number[];
  meterBuses?: number[];
  meterTrafos?: number[];
  evBuses?: number[];          // runtime DER state (falls back to the topology)
  pvBuses?: number[];
  revealTruth?: boolean;
}

const isAdditive = (e: L.LeafletMouseEvent) =>
  !!(e.originalEvent && (e.originalEvent.ctrlKey || e.originalEvent.metaKey));

const clickAt = (e: L.LeafletMouseEvent) =>
  e.originalEvent ? { x: e.originalEvent.clientX, y: e.originalEvent.clientY } : undefined;

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
export default function MapDiagram({ topo, latest, onSelectBus, onSelectLine, onSelectTrafo, batteryBuses = [], meterBuses = [], meterTrafos = [], evBuses, pvBuses, revealTruth = false }: Props) {
  const { t, i18n } = useTranslation();
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
  const meterRef = useRef<L.CircleMarker[]>([]);
  const equipRef = useRef<L.Marker[]>([]);
  const [light, setLight] = useState(true);
  const batKey = batteryBuses.join(",");
  const meterKey = `${meterBuses.join(",")}|${meterTrafos.join(",")}`;

  // voltage layers: an interconnected district is too crowded to show at once,
  // so it opens on the MV layer and the LV subgrids toggle in on demand. The
  // switch only appears when the grid really has both levels (a standalone LV
  // grid's single MV feed bus doesn't count).
  const mvBus = useMemo(
    () => new Set(topo.buses.filter((b) => b.vn_kv > 1.0).map((b) => b.id)), [topo]);
  const layered = mvBus.size > 1 && topo.buses.length - mvBus.size > 1;
  const [layer, setLayer] = useState<"mv" | "lv" | "all">("all");
  useEffect(() => { setLayer(layered ? "mv" : "all"); },
            [topo]);  // eslint-disable-line react-hooks/exhaustive-deps

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
        : { color: lineLoadingColor(null), weight: 2, opacity: 0.95 }).addTo(map);
      pl.bindTooltip(open ? t("tip.line", { name: ln.name ?? ln.id }) + t("tip.normallyOpen")
                          : t("tip.lineMap", { name: ln.name ?? ln.id }));
      pl.on("click", (e) => onSelectLineRef.current?.(ln.id, isAdditive(e), clickAt(e)));
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
      cm.bindTooltip((isExt ? t("tip.mvStation", { name: bus.name, kv: bus.vn_kv })
                     : isCab ? t("tip.cabinet", { name: bus.name, kv: bus.vn_kv })
                     : t("tip.busMap", { name: bus.name, kv: bus.vn_kv })));
      cm.on("click", (e) => onSelectRef.current?.(bus.id, isAdditive(e), clickAt(e)));
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
      cm.bindTooltip(t("tip.trafoMap", { name: tr.name ?? tr.id, kva: (tr.sn_mva * 1000).toFixed(0) }));
      cm.on("click", (e) => onSelectTrafoRef.current?.(tr.id, isAdditive(e), clickAt(e)));
      trafoRef.current.set(tr.id, cm);
    }

    const pts = [...pos.values()];
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.08));
    const timer = setTimeout(() => map.invalidateSize(), 80);

    return () => {
      clearTimeout(timer);
      map.remove();
      mapRef.current = null;
      tileRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topo, i18n.language]);   // rebuild (incl. tooltips) on language change

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

  // restyle from live results, honoring observability: only metered nodes get a
  // voltage colour; lines carry no meter (unknown) unless ground truth is revealed;
  // metered transformers colour by loading.
  useEffect(() => {
    if (!latest || !mapRef.current) return;
    const meteredBus = new Set(meterBuses);
    const meteredTrafo = new Set(meterTrafos);
    const truthLines = latest.lines ?? [];
    const maxIka = Math.max(1e-6, ...truthLines.map((l) => l.i_ka));
    const lineLive = new Map(truthLines.map((l) => [l.index, l]));
    const openLines = new Set(topo.lines.filter((l) => l.in_service === false).map((l) => l.id));
    for (const [id, pl] of lineRef.current) {
      if (openLines.has(id)) continue; // keep normally-open ties dashed grey
      const live = revealTruth ? lineLive.get(id) : undefined;
      pl.setStyle(live
        ? { color: lineLoadingColor(live.loading_percent), weight: currentWidth(live.i_ka, maxIka), opacity: 0.55 }
        : { color: UNOBSERVED_LINE, weight: 1.5, opacity: 0.8 });
    }
    const ext = new Set(topo.ext_grids.map((e) => e.bus));
    const cabs = new Set(topo.cabinet_buses ?? []);
    const measVm = new Map((latest.measurements?.nodes ?? []).map((n) => [n.bus, n.vm_pu]));
    const truthVm = new Map((latest.buses ?? []).map((b) => [b.index, b.vm_pu]));
    for (const [id, cm] of busRef.current) {
      if (ext.has(id) || cabs.has(id)) continue;  // slack + cabinets keep their symbol
      if (meteredBus.has(id)) cm.setStyle({ fillColor: voltageReds(measVm.get(id)), fillOpacity: 0.95 });
      else if (revealTruth) cm.setStyle({ fillColor: voltageReds(truthVm.get(id)), fillOpacity: 0.4 });
      else cm.setStyle({ fillColor: UNOBSERVED, fillOpacity: 0.7 });
    }
    const measTr = new Map((latest.measurements?.trafos ?? []).map((tr) => [tr.trafo, tr.loading_percent]));
    for (const [id, cm] of trafoRef.current) {
      if (meteredTrafo.has(id)) cm.setStyle({ fillColor: lineLoadingColor(measTr.get(id)), color: "#0b0d11" });
      else cm.setStyle({ fillColor: STATION_COLOR, color: "#7a5400" });
    }
  }, [latest, topo, meterKey, revealTruth]);

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
      m.bindTooltip(t("tip.battery", { bus: b.id }));
      // the marker covers the bus — forward clicks so the node stays usable
      m.on("click", (e) => onSelectRef.current?.(b.id, isAdditive(e), clickAt(e)));
      batteryRef.current.push(m);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topo, batKey, i18n.language]);

  // meter markers (blue ring) at metered buses + transformers
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    meterRef.current.forEach((m) => map.removeLayer(m));
    meterRef.current = [];
    const busPos = new Map<number, [number, number]>();
    for (const b of topo.buses) if (b.geo) busPos.set(b.id, [b.geo[1], b.geo[0]]);
    const ring = (at: [number, number], tip: string, onClick: (e: L.LeafletMouseEvent) => void) => {
      const m = L.circleMarker(at, {
        radius: 7, color: "#4c8dff", weight: 2, fill: false, opacity: 0.95,
      }).addTo(map);
      m.bindTooltip(tip);
      // the ring covers its element — forward clicks so it stays usable
      m.on("click", onClick);
      meterRef.current.push(m);
    };
    for (const bus of meterBuses) {
      const p = busPos.get(bus);
      if (p) ring(p, t("tip.metered"), (e) => onSelectRef.current?.(bus, isAdditive(e), clickAt(e)));
    }
    for (const tr of topo.trafos) {
      if (!meterTrafos.includes(tr.id)) continue;
      const at = busPos.get(tr.lv_bus) ?? busPos.get(tr.hv_bus);
      if (at) ring(at, t("tip.metered"), (e) => onSelectTrafoRef.current?.(tr.id, isAdditive(e), clickAt(e)));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topo, meterKey, i18n.language]);

  // equipment icon row per bus (battery · meter · EV charging · PV) — the same
  // glyph language as the side-panel section badges. Rebuilt when the placement
  // changes; purely decorative, never intercepts clicks.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    equipRef.current.forEach((m) => map.removeLayer(m));
    equipRef.current = [];
    const bats = new Set(batteryBuses);
    const mets = new Set(meterBuses);
    const evs = new Set(evBuses ?? topo.ev_buses ?? []);
    const pvs = new Set(pvBuses ?? topo.pv_buses ?? []);
    for (const b of topo.buses) {
      if (!b.geo) continue;
      const tags = (bats.has(b.id) ? "\u{1F50B}" : "") + (mets.has(b.id) ? "\u{1F4DF}" : "")
                 + (evs.has(b.id) ? "\u{1F50C}" : "") + (pvs.has(b.id) ? "☀️" : "");
      if (!tags) continue;
      const m = L.marker([b.geo[1], b.geo[0]], {
        icon: L.divIcon({ className: "equip-icon", html: tags, iconAnchor: [-3, 14] }),
        interactive: false, keyboard: false,
      }).addTo(map);
      equipRef.current.push(m);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topo, batKey, meterKey, (evBuses ?? []).join(","), (pvBuses ?? []).join(","), i18n.language]);

  // apply the voltage layer: hide/show buses + lines by level. Station (trafo)
  // markers stay visible in every layer — they anchor both grids. Runs after
  // every rebuild (topo / language) so visibility survives marker re-creation.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const showMv = layer !== "lv", showLv = layer !== "mv";
    const vis = (want: boolean, l: L.Path) => {
      if (want && !map.hasLayer(l)) l.addTo(map);
      else if (!want && map.hasLayer(l)) map.removeLayer(l);
    };
    for (const [id, cm] of busRef.current) vis(mvBus.has(id) ? showMv : showLv, cm);
    for (const ln of topo.lines) {
      const pl = lineRef.current.get(ln.id);
      if (pl) vis(mvBus.has(ln.from_bus) && mvBus.has(ln.to_bus) ? showMv : showLv, pl);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [layer, topo, mvBus, i18n.language]);

  return (
    <div className="map-wrap">
      <div ref={elRef} className="map-canvas" />
      <button className="map-basemap" onClick={() => setLight((v) => !v)}>
        {light ? t("map.dark") : t("map.light")}
      </button>
      {layered && (
        <div className="map-layers">
          {(["mv", "lv", "all"] as const).map((k) => (
            <button key={k} className={layer === k ? "on" : ""}
                    title={t(`map.layer${k[0].toUpperCase()}${k.slice(1)}Title`)}
                    onClick={() => setLayer(k)}>
              {t(`map.layer${k[0].toUpperCase()}${k.slice(1)}`)}
            </button>
          ))}
        </div>
      )}
      <div className="map-colorbars">
        <Colorbar gradient={LOADING_GRADIENT} top="100%" bottom="0%" caption={t("map.lineLoading")} />
        <Colorbar gradient={REDS_GRADIENT} top="±6%" bottom="0%" caption={t("map.busVolt")} />
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
