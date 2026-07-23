import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { StepResult, Topology } from "../types";
import { currentWidth, fmt, lineLoadingColor, voltageReds, LOADING_GRADIENT, REDS_GRADIENT, UNOBSERVED, UNOBSERVED_LINE } from "../scales";

interface Props {
  topo: Topology;
  latest: StepResult | null;
  onSelectBus?: (bus: number, additive: boolean, at?: { x: number; y: number }) => void;
  onSelectLine?: (line: number, additive: boolean, at?: { x: number; y: number }) => void;
  onSelectTrafo?: (trafo: number, additive: boolean, at?: { x: number; y: number }) => void;
  batteryBuses?: number[];
  controllerBuses?: number[];   // 🎛 overload controllers (station = LV busbar)
  signalBuses?: number[];       // 🚦 stations whose Steuerbox is dimming → red ring
  focusBuses?: number[];        // drill-down: zoom the map to these buses (a cell)
  selectedBuses?: number[];     // pinned sections (Übersicht) → gold ring
  selectedTrafos?: number[];
  meterBuses?: number[];
  meterTrafos?: number[];
  evBuses?: number[];          // runtime DER state (falls back to the topology)
  pvBuses?: number[];
  extFeedBuses?: number[];     // 📡 external nodes (live P/Q feed)
  revealTruth?: boolean;
}

const isAdditive = (e: L.LeafletMouseEvent) =>
  !!(e.originalEvent && (e.originalEvent.ctrlKey || e.originalEvent.metaKey));

const clickAt = (e: L.LeafletMouseEvent) =>
  e.originalEvent ? { x: e.originalEvent.clientX, y: e.originalEvent.clientY } : undefined;

// live-value popup helpers (the popup content is a plain HTML string — same
// pattern as the rtheatflow sibling: only topology names get escaped)
const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const row = (k: string, v: string) =>
  `<span style="color:var(--muted)">${k}</span>&thinsp;${v}`;
const muted = (s: string) => `<span style="color:var(--muted)">${s}</span>`;
// unit-scaled quantities: LV values read best in kW/kvar/kVA, district MV in MW
const pRow = (mw: number | null | undefined) =>
  mw == null ? "–" : Math.abs(mw) < 1 ? `${fmt(mw * 1000, 1)} kW` : `${fmt(mw, 2)} MW`;
const qRow = (mvar: number | null | undefined) =>
  mvar == null ? "–" : Math.abs(mvar) < 1 ? `${fmt(mvar * 1000, 1)} kvar` : `${fmt(mvar, 2)} Mvar`;
const iRow = (ka: number | null | undefined) =>
  ka == null ? "–" : `${fmt(ka * 1000, 0)} A`;
// losses are often single watts on LV cables — show W below 1 kW so a lightly
// loaded line reads "36 W" instead of a misleading "0 kW"
const lossRow = (mw: number | null | undefined) =>
  mw == null ? "–" : Math.abs(mw) < 0.001 ? `${fmt(mw * 1e6, 0)} W` : pRow(mw);
const uRow = (vmPu: number | null | undefined, vnKv: number) =>
  vmPu == null ? "–"
    : vnKv < 1
      ? `${fmt(vmPu * vnKv * 1000, 1)} V (${fmt(vmPu * 100, 1)} %)`
      : `${fmt(vmPu * vnKv, 2)} kV (${fmt(vmPu * 100, 1)} %)`;

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
export default function MapDiagram({ topo, latest, onSelectBus, onSelectLine, onSelectTrafo, batteryBuses = [], controllerBuses = [], signalBuses = [], focusBuses = [], selectedBuses = [], selectedTrafos = [], meterBuses = [], meterTrafos = [], evBuses, pvBuses, extFeedBuses = [], revealTruth = false }: Props) {
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

  // ---- live-value popups (click a node/line/trafo) ------------------------ //
  // The frame arrives view-spliced from LivePowerFlow (estimate mode replaces
  // the truth arrays), so the popup honors the operator's perspective exactly
  // like the marker coloring: meter reading first, else the frame value when
  // the view may reveal it, else honestly "unknown". Content closures read
  // liveRef so an OPEN popup keeps ticking with every WS frame.
  const liveRef = useRef({ latest, revealTruth, meterBuses, meterTrafos });
  liveRef.current = { latest, revealTruth, meterBuses, meterTrafos };

  const busPopup = (bus: Topology["buses"][number]): string => {
    const { latest: f, revealTruth: reveal, meterBuses: mb } = liveRef.current;
    const slack = topo.ext_grids.find((e) => e.bus === bus.id);
    let head = `<b>${esc(bus.name)}</b><br>` + muted(
      `Uₙ ${bus.vn_kv < 1 ? `${fmt(bus.vn_kv * 1000, 0)} V` : `${fmt(bus.vn_kv, 0)} kV`}`
      + (slack ? ` · ${t("pop.slack")}` : ""));
    if (!f) return `${head}<br>${muted(t("pop.noData"))}`;
    if (slack && reveal) {
      const eg = (f.ext_grids ?? []).find((x) => x.index === slack.id);
      if (eg) head += `<br>${row(t("pop.exchange"), `${pRow(eg.p_mw)} · ${qRow(eg.q_mvar)}`)}`;
    }
    const reading = (f.measurements?.nodes ?? []).find((n) => n.bus === bus.id);
    if (mb.includes(bus.id)) {
      if (!reading) return `${head}<br>📟 ${muted(t("pop.coldStart"))}`;
      return `${head}<br>📟 ${row(t("pop.u"), uRow(reading.vm_pu, bus.vn_kv))}`
        + `<br>${row("P", pRow(reading.p_mw))} · ${row("Q", qRow(reading.q_mvar))}`
        + `<br>${row("I", iRow(reading.i_ka))}`;
    }
    const b = (f.buses ?? []).find((x) => x.index === bus.id);
    if (reveal && b) {
      return `${head}<br>${row(t("pop.u"), uRow(b.vm_pu, bus.vn_kv))}`
        + `<br>${row("P", pRow(b.p_mw))} · ${row("Q", qRow(b.q_mvar))}`;
    }
    return `${head}<br>${muted(t("pop.unobserved"))}`;
  };

  const linePopup = (ln: Topology["lines"][number]): string => {
    const { latest: f, revealTruth: reveal } = liveRef.current;
    const len = ln.length_km < 1
      ? `${fmt(ln.length_km * 1000, 0)} m` : `${fmt(ln.length_km, 2)} km`;
    const head = `<b>${esc(ln.name ?? `${t("pop.line")} ${ln.id}`)}</b><br>`
      + muted(`${t("pop.length")} ${len}`
        + (ln.in_service === false ? ` · ${t("pop.openTie")}` : ""));
    if (!f) return `${head}<br>${muted(t("pop.noData"))}`;
    const l = (f.lines ?? []).find((x) => x.index === ln.id);
    if (reveal && l) {
      return `${head}<br>${row(t("pop.loading"), `${fmt(l.loading_percent, 1)} %`)} · `
        + row("I", iRow(l.i_ka))
        + `<br>${row("P", pRow(l.p_from_mw))} · ${row(t("pop.losses"), lossRow(l.pl_mw))}`;
    }
    return `${head}<br>${muted(t("pop.lineUnobserved"))}`;
  };

  const trafoPopup = (tr: Topology["trafos"][number]): string => {
    const { latest: f, revealTruth: reveal, meterTrafos: mt } = liveRef.current;
    const head = `<b>${esc(tr.name ?? `${t("pop.trafo")} ${tr.id}`)}</b><br>`
      + muted(`${t("pop.rating")} ${fmt(tr.sn_mva * 1000, 0)} kVA`);
    if (!f) return `${head}<br>${muted(t("pop.noData"))}`;
    const rows = (x: { loading_percent: number | null; p_hv_mw: number | null;
                       q_hv_mvar: number | null; i_hv_ka: number | null;
                       pl_mw?: number | null }, tag: string) =>
      `${head}<br>${tag}${row(t("pop.loading"), `${fmt(x.loading_percent, 1)} %`)} · `
      + row("I", iRow(x.i_hv_ka))
      + `<br>${row("P", pRow(x.p_hv_mw))} · ${row("Q", qRow(x.q_hv_mvar))}`
      + `<br>${row(t("pop.losses"), lossRow(x.pl_mw))}`;
    if (mt.includes(tr.id)) {
      const reading = (f.measurements?.trafos ?? []).find((x) => x.trafo === tr.id);
      if (!reading) return `${head}<br>📟 ${muted(t("pop.coldStart"))}`;
      return rows(reading, "📟 ");
    }
    const x = (f.trafos ?? []).find((y) => y.index === tr.id);
    if (reveal && x) return rows(x, "");
    return `${head}<br>${muted(t("pop.unobserved"))}`;
  };

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
    map.zoomControl.setPosition("topleft");      // +/- in the upper-left corner
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
      // click = live-value popup (rtheatflow grammar) · ctrl = pin · right = menu
      pl.bindPopup(() => linePopup(ln), { autoPan: false });
      pl.on("click", (e) => {
        if (isAdditive(e)) { pl.closePopup(); onSelectLineRef.current?.(ln.id, true, clickAt(e)); }
      });
      pl.on("contextmenu", (e) => {
        e.originalEvent?.preventDefault();
        onSelectLineRef.current?.(ln.id, false, clickAt(e));
      });
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
      cm.bindPopup(() => busPopup(bus), { autoPan: false });
      cm.on("click", (e) => {
        if (isAdditive(e)) { cm.closePopup(); onSelectRef.current?.(bus.id, true, clickAt(e)); }
      });
      cm.on("contextmenu", (e) => {
        e.originalEvent?.preventDefault();
        onSelectRef.current?.(bus.id, false, clickAt(e));
      });
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
      cm.bindPopup(() => trafoPopup(tr), { autoPan: false });
      cm.on("click", (e) => {
        if (isAdditive(e)) { cm.closePopup(); onSelectTrafoRef.current?.(tr.id, true, clickAt(e)); }
      });
      cm.on("contextmenu", (e) => {
        e.originalEvent?.preventDefault();
        onSelectTrafoRef.current?.(tr.id, false, clickAt(e));
      });
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

  // drill-down: zoom to a cell's buses; clearing the focus zooms back out to
  // the whole grid (only on the transition, so manual panning stays free)
  const focusKey = focusBuses.join(",");
  const hadFocus = useRef(false);
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const pts = focusBuses
      .map((b) => busRef.current.get(b)?.getLatLng())
      .filter((p): p is L.LatLng => !!p);
    if (pts.length) {
      map.fitBounds(L.latLngBounds(pts).pad(0.35));
      hadFocus.current = true;
    } else if (hadFocus.current) {
      const all = [...busRef.current.values()].map((cm) => cm.getLatLng());
      if (all.length) map.fitBounds(L.latLngBounds(all).pad(0.1));
      hadFocus.current = false;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusKey]);

  // pinned sections (their graphs sit in the side panel) get the same gold
  // ring as in the schematic — declared AFTER the theme effect so the ring
  // survives a light/dark flip (both write the outline color)
  const selKey = `${selectedBuses.join(",")}|${selectedTrafos.join(",")}|${signalBuses.join(",")}`;
  useEffect(() => {
    const theme = light ? TILES.light : TILES.dark;
    const ext = new Set(topo.ext_grids.map((e) => e.bus));
    const cabs = new Set(topo.cabinet_buses ?? []);
    const selB = new Set(selectedBuses);
    const sigB = new Set(signalBuses);   // Netzampel: dimming Steuerbox → red ring
    for (const [id, cm] of busRef.current) {
      const isExt = ext.has(id);
      const isCab = cabs.has(id);
      if (selB.has(id)) {
        cm.setStyle({ color: "#ffd166", weight: 3 });
        cm.setRadius(isExt ? 9 : isCab ? 7 : 6);
      } else if (sigB.has(id)) {
        cm.setStyle({ color: "#f85149", weight: 3 });
        cm.setRadius(isExt ? 9 : isCab ? 7 : 6);
      } else {
        cm.setStyle({ color: isExt ? "#7a5400" : isCab ? "#1a7a1a" : theme.stroke,
                      weight: isExt ? 1.5 : isCab ? 2 : 0.5 });
        cm.setRadius(isExt ? 7 : isCab ? 5 : 3);
      }
    }
    const selT = new Set(selectedTrafos);
    for (const [id, cm] of trafoRef.current) {
      if (selT.has(id)) {
        cm.setStyle({ color: "#ffd166", weight: 3 });
        cm.setRadius(6);
      } else {
        cm.setStyle({ color: "#7a5400", weight: 1 });
        cm.setRadius(4);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selKey, light, topo]);

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
    // an OPEN live-value popup ticks along with every frame (max one is open)
    for (const [id, cm] of busRef.current) {
      if (!cm.isPopupOpen()) continue;
      const b = topo.buses.find((x) => x.id === id);
      if (b) cm.setPopupContent(busPopup(b));
    }
    for (const [id, pl] of lineRef.current) {
      if (!pl.isPopupOpen()) continue;
      const ln = topo.lines.find((x) => x.id === id);
      if (ln) pl.setPopupContent(linePopup(ln));
    }
    for (const [id, cm] of trafoRef.current) {
      if (!cm.isPopupOpen()) continue;
      const tr = topo.trafos.find((x) => x.id === id);
      if (tr) cm.setPopupContent(trafoPopup(tr));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      // the marker covers the bus — forward interactions so the node stays
      // usable (plain click opens the bus's live-value popup)
      m.on("click", (e) => {
        if (isAdditive(e)) onSelectRef.current?.(b.id, true, clickAt(e));
        else busRef.current.get(b.id)?.openPopup();
      });
      m.on("contextmenu", (e) => {
        e.originalEvent?.preventDefault();
        onSelectRef.current?.(b.id, false, clickAt(e));
      });
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
    const ring = (at: [number, number], tip: string, openPopup: () => void,
                  forward: (additive: boolean, at?: { x: number; y: number }) => void) => {
      const m = L.circleMarker(at, {
        radius: 7, color: "#4c8dff", weight: 2, fill: false, opacity: 0.95,
      }).addTo(map);
      m.bindTooltip(tip);
      // the ring covers its element — forward interactions so it stays usable
      m.on("click", (e) => { if (isAdditive(e)) forward(true, clickAt(e)); else openPopup(); });
      m.on("contextmenu", (e) => { e.originalEvent?.preventDefault(); forward(false, clickAt(e)); });
      meterRef.current.push(m);
    };
    for (const bus of meterBuses) {
      const p = busPos.get(bus);
      if (p) ring(p, t("tip.metered"),
                  () => busRef.current.get(bus)?.openPopup(),
                  (add, at) => onSelectRef.current?.(bus, add, at));
    }
    for (const tr of topo.trafos) {
      if (!meterTrafos.includes(tr.id)) continue;
      const at = busPos.get(tr.lv_bus) ?? busPos.get(tr.hv_bus);
      if (at) ring(at, t("tip.metered"),
                   () => trafoRef.current.get(tr.id)?.openPopup(),
                   (add, at2) => onSelectTrafoRef.current?.(tr.id, add, at2));
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
    const ctrls = new Set(controllerBuses);
    const mets = new Set(meterBuses);
    const exts = new Set(extFeedBuses);
    const evs = new Set(evBuses ?? topo.ev_buses ?? []);
    const pvs = new Set(pvBuses ?? topo.pv_buses ?? []);
    for (const b of topo.buses) {
      if (!b.geo) continue;
      const tags = (bats.has(b.id) ? "\u{1F50B}" : "") + (ctrls.has(b.id) ? "\u{1F39B}️" : "")
                 + (mets.has(b.id) ? "\u{1F4DF}" : "") + (exts.has(b.id) ? "\u{1F4E1}" : "")
                 + (evs.has(b.id) ? "\u{1F50C}" : "") + (pvs.has(b.id) ? "☀️" : "");
      if (!tags) continue;
      const m = L.marker([b.geo[1], b.geo[0]], {
        icon: L.divIcon({ className: "equip-icon", html: tags, iconAnchor: [-3, 14] }),
        interactive: false, keyboard: false,
      }).addTo(map);
      equipRef.current.push(m);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topo, batKey, controllerBuses.join(","), meterKey, (evBuses ?? []).join(","), (pvBuses ?? []).join(","), extFeedBuses.join(","), i18n.language]);

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
        <Colorbar gradient={REDS_GRADIENT} top="±10%" bottom="0%" caption={t("map.busVolt")} />
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
