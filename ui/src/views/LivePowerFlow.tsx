import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Battery, BatteryMode, EngineStatus, GridController, MeasurementsResponse, MeterMode, MeterPreset, NodeMeasurement, Topology, TrafoMeasurement } from "../types";
import { useStepStream } from "../useWebSocket";
import { fmt, loadingColor, voltageColor, V_BASE } from "../scales";
import GridDiagram from "../components/GridDiagram";
import MapDiagram from "../components/MapDiagram";
import NodeProfile from "../components/NodeProfile";
import LineProfile from "../components/LineProfile";
import TrafoProfile from "../components/TrafoProfile";
import BatteryProfile from "../components/BatteryProfile";
import MeasurementPanel from "../components/MeasurementPanel";
import ElementMenu, { type MenuTarget } from "../components/ElementMenu";
import DerPanel from "../components/DerPanel";
import Section from "../components/Section";
import { gridDisplayName } from "../gridname";
import type { LiveView } from "../App";

type SelKind = "bus" | "line" | "trafo";
// One collapsible side-panel section per grid element. A section exists while
// pinned (via the element menu / ctrl-click) or while a battery sits at the
// element; its body (graphs, readings) renders lazily on expand.
interface Sec { kind: SelKind; id: number; open: boolean; }

export default function LivePowerFlow({ onActive, view, onView, measStamp }: {
  onActive: () => void;
  view: LiveView;                              // display settings live in App
  onView: (patch: Partial<LiveView>) => void;  // (driven by the Ansicht menu)
  measStamp: number;                           // menu meter actions -> refetch
}) {
  const { t } = useTranslation();
  const { layout, showValues, viewMode } = view;
  const [topo, setTopo] = useState<Topology | null>(null);
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sections, setSections] = useState<Sec[]>([]);
  const [menu, setMenu] = useState<MenuTarget | null>(null);
  const [ovOpen, setOvOpen] = useState(true);          // "Overview" section
  const [measOpen, setMeasOpen] = useState(false);     // bulk "Measurements" section
  const [stepSeconds, setStepSeconds] = useState(1);   // accelerated-tick interval (s/step)
  const [pvDates, setPvDates] = useState<string[]>([]); // real-PV day calendar (day slider)
  const [sideW, setSideW] = useState(340);             // resizable overview width (px)
  const [batteries, setBatteries] = useState<Battery[]>([]);
  const [controllers, setControllers] = useState<GridController[]>([]);
  const [batModes, setBatModes] = useState<BatteryMode[]>([]);
  const [batHasPrices, setBatHasPrices] = useState(false);
  const [placement, setPlacement] = useState<MeasurementsResponse | null>(null);  // meter placement
  const [gridId, setGridId] = useState<string | null>(null);   // active grid id -> localized name
  // runtime DER state: which buses host EV charging / PV (seeded from the
  // topology, extended live on add) + a stamp that refreshes graphs/panels
  const [evBuses, setEvBuses] = useState<number[]>([]);
  const [pvBuses, setPvBuses] = useState<number[]>([]);
  const [derStamp, setDerStamp] = useState(0);
  // Three parallel views of the grid: ground truth (default, so a fresh session
  // sees a normal colored grid), the strict observed-only projection, and the
  // WLS state estimate computed from the placed meters (estimator.py).
  const layoutInit = useRef(false);
  const intervalInit = useRef(false);

  const { latest, status: wsStatus } = useStepStream(true);

  const loadTopo = () => api.network().then(setTopo).catch((e) => setError(String(e)));
  const loadStatus = () => api.status().then(setStatus).catch(() => {});
  const reloadBatteries = () => api.batteries()
    .then((r) => { setBatteries(r.batteries); setBatModes(r.modes); setBatHasPrices(r.has_prices); })
    .catch(() => {});
  const reloadControllers = () => api.controllers()
    .then((r) => setControllers(r.controllers)).catch(() => {});
  const reloadMeasurements = () => api.measurements().then(setPlacement).catch(() => {});

  useEffect(() => {
    loadTopo();
    loadStatus();
    onActive();
    api.pvDays().then((r) => setPvDates(r.dates)).catch(() => {});
    api.active().then((a) => setGridId(a.grid_id)).catch(() => {});
    reloadBatteries();
    reloadControllers();
    reloadMeasurements();
    const t = setInterval(loadStatus, 2000);
    return () => clearInterval(t);
  }, []);

  // seed the runtime DER state from the topology
  useEffect(() => {
    setEvBuses(topo?.ev_buses ?? []);
    setPvBuses(topo?.pv_buses ?? []);
  }, [topo]);

  // default to the real OSM map for grids that carry geo-coordinates, else schematic
  useEffect(() => {
    if (topo && !layoutInit.current) {
      layoutInit.current = true;
      onView({ layout: topo.has_geo ? "map" : "tree",
               showValues: topo.buses.length <= 40 });  // values on by default for small grids
    }
  }, [topo]);

  // meter changes made in the menu bar (presets, TAF mode) -> refresh placement
  useEffect(() => {
    if (measStamp > 0) reloadMeasurements();
  }, [measStamp]);

  // drop stale sections when the grid changes; batteries + meters reset with it
  useEffect(() => { setSections([]); setMenu(null); reloadBatteries(); reloadControllers(); reloadMeasurements(); }, [topo?.name]);

  // adopt the engine's current tick interval once, then it's user-driven
  useEffect(() => {
    if (status && !intervalInit.current) { intervalInit.current = true; setStepSeconds(status.interval_seconds); }
  }, [status]);

  // ---- section bookkeeping ------------------------------------------------ //
  const busForElement = (kind: SelKind, id: number): number | null =>
    kind === "bus" ? id
      : kind === "trafo" ? topo?.trafos.find((tr) => tr.id === id)?.lv_bus ?? null
      : null;
  const batteryAt = (kind: SelKind, id: number): Battery | undefined => {
    const bus = busForElement(kind, id);
    return bus == null ? undefined : batteries.find((b) => b.bus === bus);
  };
  // a station controller lives on the trafo, a bus controller on its node
  const controllerAt = (kind: SelKind, id: number): GridController | undefined =>
    kind === "trafo" ? controllers.find((c) => c.scope === "station")
      : kind === "bus" ? controllers.find((c) => c.scope === "bus" && c.bus === id)
      : undefined;

  const pinSection = (kind: SelKind, id: number) =>
    setSections((prev) => {
      const i = prev.findIndex((s) => s.kind === kind && s.id === id);
      if (i >= 0) return prev.map((s, j) => (j === i ? { ...s, open: true } : s));
      return [...prev, { kind, id, open: true }];
    });
  const toggleOpen = (kind: SelKind, id: number) =>
    setSections((prev) => prev.map((s) => (s.kind === kind && s.id === id ? { ...s, open: !s.open } : s)));
  const closeSection = (kind: SelKind, id: number) =>
    setSections((prev) => prev.filter((s) => !(s.kind === kind && s.id === id)));

  // a battery keeps its element's section alive (collapse-only); make sure one
  // exists for every battery not already covered (e.g. after a reload)
  useEffect(() => {
    if (!topo) return;
    setSections((prev) => {
      const covered = (bus: number) => prev.some((s) =>
        (s.kind === "bus" && s.id === bus) ||
        (s.kind === "trafo" && topo.trafos.find((tr) => tr.id === s.id)?.lv_bus === bus));
      const add = batteries.filter((b) => !covered(b.bus))
        .map((b) => ({ kind: "bus" as const, id: b.bus, open: false }));
      // controllers keep a section alive too (station -> its trafo section)
      const ctrlSecs = controllers
        .map((c) => (c.scope === "bus"
          ? { kind: "bus" as const, id: c.bus ?? -1, open: false }
          : topo.trafos[0] ? { kind: "trafo" as const, id: topo.trafos[0].id, open: false } : null))
        .filter((s): s is { kind: "bus" | "trafo"; id: number; open: boolean } =>
          s !== null && s.id >= 0
          && !prev.some((p) => p.kind === s.kind && p.id === s.id)
          && !add.some((a) => a.kind === s.kind && a.id === s.id));
      return add.length || ctrlSecs.length ? [...prev, ...add, ...ctrlSecs] : prev;
    });
  }, [batteries, controllers, topo]);

  // ---- element clicks: plain click opens the action menu, ctrl-click pins -- //
  const elemName = (kind: SelKind, id: number): string => {
    if (!topo) return String(id);
    if (kind === "bus") return topo.buses.find((b) => b.id === id)?.name ?? String(id);
    if (kind === "line") return topo.lines.find((l) => l.id === id)?.name ?? String(id);
    return topo.trafos.find((tr) => tr.id === id)?.name ?? String(id);
  };
  const elemTitle = (kind: SelKind, id: number): string =>
    t(`${kind === "bus" ? "node" : kind}.title`, { name: elemName(kind, id) });

  const onElemClick = (kind: SelKind) => (id: number, additive: boolean, at?: { x: number; y: number }) => {
    if (additive || !at) {
      // ctrl/⌘-click: pin/unpin the daily-profile section directly
      setSections((prev) => {
        const i = prev.findIndex((s) => s.kind === kind && s.id === id);
        if (i < 0) return [...prev, { kind, id, open: true }];
        if (batteryAt(kind, id)) return prev.map((s, j) => (j === i ? { ...s, open: !s.open } : s));
        return prev.filter((_, j) => j !== i);
      });
      return;
    }
    setMenu({ kind, id, name: elemTitle(kind, id), x: at.x, y: at.y });
  };
  const selectBus = onElemClick("bus");
  const selectLine = onElemClick("line");
  const selectTrafo = onElemClick("trafo");

  // ---- equipment actions (from the menu / the element sections) ------------ //
  const addBatteryAt = async (kind: SelKind, id: number) => {
    const bus = busForElement(kind, id);
    if (bus == null) return;
    // deploys with the default strategy; switched later in the node's section
    try { await api.addBattery({ bus, capacity_kwh: 10, power_kw: 5, mode: "self" }); } finally { reloadBatteries(); }
    pinSection(kind, id);
  };
  const addControllerAt = async (kind: SelKind, id: number) => {
    try {
      if (kind === "trafo") await api.addController({ scope: "station" });
      else if (kind === "bus") await api.addController({ scope: "bus", bus: id });
      else return;
    } finally { reloadControllers(); }
    pinSection(kind, id);
  };
  const removeControllerById = async (cid: number) => {
    try { await api.removeController(cid); } finally { reloadControllers(); }
  };
  const setControllerLimitAt = async (cid: number, pct: number) => {
    try {
      const r = await api.setControllerLimit(cid, pct);
      setControllers(r.controllers);
    } catch { reloadControllers(); }
  };
  const addPvAt = async (bus: number) => {
    await api.addPv(bus, 5);
    setPvBuses((prev) => (prev.includes(bus) ? prev : [...prev, bus]));
    setDerStamp((v) => v + 1);
    pinSection("bus", bus);
  };
  const addEvAt = async (bus: number) => {
    await api.addEv(bus);
    setEvBuses((prev) => (prev.includes(bus) ? prev : [...prev, bus]));
    setDerStamp((v) => v + 1);
    pinSection("bus", bus);
  };
  const removePvAt = async (bus: number) => {
    const der = await api.nodeDer(bus);
    if (der.pv) await api.removePv(der.pv.sgen);
    setPvBuses((prev) => prev.filter((b) => b !== bus));
    setDerStamp((v) => v + 1);
  };
  const removeEvAt = async (bus: number) => {
    const der = await api.nodeDer(bus);
    if (der.ev) await api.removeEv(der.ev.load);
    setEvBuses((prev) => prev.filter((b) => b !== bus));
    setDerStamp((v) => v + 1);
  };
  const changeBatteryMode = async (index: number, mode: BatteryMode) => {
    try {
      const r = await api.setBatteryMode(index, mode);
      setBatteries(r.batteries);
    } catch { reloadBatteries(); }
  };
  const changeBatterySize = async (index: number, kwh: number, kw: number) => {
    try {
      const r = await api.setBatterySize(index, kwh, kw);
      setBatteries(r.batteries);
    } catch { reloadBatteries(); }
  };
  const removeBattery = async (idx: number) => {
    try { await api.removeBattery(idx); } finally { reloadBatteries(); }
  };
  const placeMeterAt = async (kind: SelKind, id: number) => {
    if (kind === "bus") setPlacement(await api.placeNodeMeter(id));
    else if (kind === "trafo") setPlacement(await api.placeTrafoMeter(id));
    pinSection(kind, id);
  };
  const removeMeterAt = async (kind: SelKind, id: number) => {
    if (kind === "bus") setPlacement(await api.removeNodeMeter(id));
    else if (kind === "trafo") setPlacement(await api.removeTrafoMeter(id));
  };
  const meterPreset = async (name: MeterPreset) => setPlacement(await api.meterPreset(name));
  const meterMode = async (name: MeterMode) => setPlacement(await api.meterMode(name));

  const toggleRun = async () => {
    setStatus(status?.running ? await api.pause() : await api.start());
  };
  const seek = async (step: number) => setStatus(await api.seek(step));
  const changeInterval = async (s: number) => { setStepSeconds(s); setStatus(await api.stepInterval(s)); };
  const seekDay = async (d: number) => setStatus(await api.seekDay(d));

  if (error) return <div className="empty">{t("live.failedNet")}<br />{error}</div>;
  if (!topo) return <div className="spinner">{t("live.loadingNet")}</div>;

  const s = latest?.summary;
  const step = latest?.step ?? status?.step ?? 0;
  const spd = topo.steps_per_day || status?.steps_per_day || 1440;
  // current time of day as a 0..1 fraction, for the "now" marker on the graphs
  const nowFrac = latest && spd > 1 ? latest.step / (spd - 1) : null;
  // real-PV day: use the per-step value while running, else the (seek-updated) status
  const nDays = status?.n_days ?? 1;
  const curDay = (latest && status?.running) ? latest.day : (status?.day ?? latest?.day ?? 0);
  const dayIdx = nDays > 0 ? ((curDay % nDays) + nDays) % nDays : 0;

  const selBuses = sections.filter((x) => x.kind === "bus").map((x) => x.id);
  const selLines = sections.filter((x) => x.kind === "line").map((x) => x.id);
  const selTrafos = sections.filter((x) => x.kind === "trafo").map((x) => x.id);

  const batLive: Record<number, { soc_percent: number; p_mw: number }> = {};
  latest?.batteries?.forEach((b) => { batLive[b.index] = { soc_percent: b.soc_percent, p_mw: b.p_mw }; });
  const batteryBuses = batteries.map((b) => b.bus);

  // observability: placed meters + live readings
  const meterBuses = placement?.node_buses ?? [];
  const meterTrafos = placement?.trafo_idxs ?? [];
  const canReveal = placement?.expose_ground_truth ?? false;
  const est = latest?.estimated ?? null;
  // fall back gracefully when the chosen view is unavailable (strict server /
  // no meters placed yet)
  const mode = viewMode === "truth" && !canReveal ? "observed"
    : viewMode === "est" && !est ? (canReveal ? "truth" : "observed")
    : viewMode;
  const reveal = mode === "truth";
  // in estimate mode the diagrams render the estimated arrays through the same
  // code path as ground truth (values keyed by element index)
  const frame = mode === "est" && latest && est
    ? ({ ...latest, buses: est.buses, lines: est.lines, trafos: est.trafos } as unknown as typeof latest)
    : latest;
  const liveNodeMeas = new Map<number, NodeMeasurement>();
  latest?.measurements?.nodes.forEach((n) => liveNodeMeas.set(n.bus, n));
  const liveTrafoMeas = new Map<number, TrafoMeasurement>();
  latest?.measurements?.trafos.forEach((tr) => liveTrafoMeas.set(tr.trafo, tr));
  const os = latest?.observed_summary;

  const meteredElem = (kind: SelKind, id: number) =>
    kind === "bus" ? meterBuses.includes(id) : kind === "trafo" ? meterTrafos.includes(id) : false;

  // menu context (only while open)
  const menuBattery = menu ? batteryAt(menu.kind, menu.id) : undefined;
  const menuController = menu ? controllerAt(menu.kind, menu.id) : undefined;
  const menuMetered = menu ? meteredElem(menu.kind, menu.id) : false;

  // 🎛 badge positions: bus controllers at their node, the station controller
  // at the LV busbar of the (first) transformer
  const controllerBuses = controllers
    .map((c) => (c.scope === "bus" ? c.bus ?? -1 : topo?.trafos[0]?.lv_bus ?? -1))
    .filter((b) => b >= 0);

  // drag the panel's left edge to widen it (and the graphs, which are width:100%)
  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    const move = (ev: MouseEvent) => setSideW(Math.min(760, Math.max(260, window.innerWidth - ev.clientX)));
    const up = () => { window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up); };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  };

  return (
    <div className="live" style={{ gridTemplateColumns: `1fr ${sideW}px` }}>
      <div className="diagram-wrap">
        {layout === "map" && topo.has_geo ? (
          <MapDiagram topo={topo} latest={frame} batteryBuses={batteryBuses} onSelectBus={selectBus}
                      controllerBuses={controllerBuses}
                      onSelectLine={selectLine} onSelectTrafo={selectTrafo}
                      evBuses={evBuses} pvBuses={pvBuses}
                      meterBuses={meterBuses} meterTrafos={meterTrafos} revealTruth={mode !== "observed"} />
        ) : (
          <GridDiagram topo={topo} latest={frame} showValues={showValues} batteryBuses={batteryBuses}
                       controllerBuses={controllerBuses}
                       onSelectBus={selectBus} selectedBuses={selBuses}
                       onSelectLine={selectLine} selectedLines={selLines}
                       onSelectTrafo={selectTrafo} selectedTrafos={selTrafos}
                       evBuses={evBuses} pvBuses={pvBuses}
                       meterBuses={meterBuses} meterTrafos={meterTrafos} revealTruth={mode !== "observed"} />
        )}
      </div>

      {menu && (
        <ElementMenu
          target={menu}
          hasBattery={!!menuBattery}
          hasMeter={menuMetered}
          hasPv={menu.kind === "bus" && pvBuses.includes(menu.id)}
          hasEv={menu.kind === "bus" && evBuses.includes(menu.id)}
          hasController={!!menuController}
          onAddController={() => addControllerAt(menu.kind, menu.id)}
          onRemoveController={() => menuController && removeControllerById(menuController.id)}
          onGraph={() => pinSection(menu.kind, menu.id)}
          onAddBattery={() => addBatteryAt(menu.kind, menu.id)}
          onAddPv={() => { if (menu.kind === "bus") addPvAt(menu.id); }}
          onAddEv={() => { if (menu.kind === "bus") addEvAt(menu.id); }}
          onRemovePv={() => { if (menu.kind === "bus") removePvAt(menu.id); }}
          onRemoveEv={() => { if (menu.kind === "bus") removeEvAt(menu.id); }}
          onRemoveBattery={() => menuBattery && removeBattery(menuBattery.index)}
          onPlaceMeter={() => placeMeterAt(menu.kind, menu.id)}
          onRemoveMeter={() => removeMeterAt(menu.kind, menu.id)}
          onClose={() => setMenu(null)}
        />
      )}

      <aside className="side">
        <div className="side-resizer" onMouseDown={startResize} />
        <div className="clock">
          {latest ? t("live.day", { day: latest.day, time: latest.time_of_day }) : "—"}
          {latest && !latest.converged && <span className="note">{t("live.notConverged")}</span>}
        </div>
        <div className="muted" style={{ fontSize: "0.75rem", marginBottom: "0.2rem" }}>
          {t("live.gridInfo", { name: gridDisplayName(gridId, topo.name, t), buses: topo.buses.length, ws: wsStatus })}
        </div>

        <Section title={t("sec.overview")} open={ovOpen} onToggle={() => setOvOpen((v) => !v)}>
          {mode === "est" && est ? (
            // the operator's calculated view: aggregates over the WLS estimate
            <>
              <div className="muted" style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 2 }}>
                🧮 {t("live.estCaption")}
              </div>
              <Stat label={t("live.vminmax")}
                    value={(() => { const v = est.buses.map((b) => b.vm_pu).filter((x): x is number => x != null);
                                    return v.length ? `${fmt(Math.min(...v) * V_BASE, 1)} / ${fmt(Math.max(...v) * V_BASE, 1)} V` : t("live.na"); })()} />
              <Stat label={t("live.maxLine")}
                    value={(() => { const v = est.lines.map((l) => l.loading_percent).filter((x): x is number => x != null);
                                    return v.length ? `${fmt(Math.max(...v), 1)} %` : t("live.na"); })()}
                    color={loadingColor(Math.max(0, ...est.lines.map((l) => l.loading_percent ?? 0)))} />
              <Stat label={t("live.maxTrafo")}
                    value={(() => { const v = est.trafos.map((tr) => tr.loading_percent).filter((x): x is number => x != null);
                                    return v.length ? `${fmt(Math.max(...v), 1)} %` : t("live.na"); })()}
                    color={loadingColor(Math.max(0, ...est.trafos.map((tr) => tr.loading_percent ?? 0)))} />
              {est.error?.max_dv_pu != null && (
                <Stat label={t("live.estErrV")} value={`${fmt(est.error.max_dv_pu * V_BASE, 2)} V`} />
              )}
              <Stat label={t("live.estSolve")} value={`${fmt(est.solve_ms, 0)} ms`} />
            </>
          ) : reveal && s ? (
            // ground truth (revealed): the true system-wide summary
            <>
              <div className="muted" style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 2 }}>
                👁 {t("live.groundTruth")}
              </div>
              <Stat label={t("live.vminmax")} value={`${fmt(s.vm_pu_min * V_BASE, 1)} / ${fmt(s.vm_pu_max * V_BASE, 1)} V`} />
              <Stat label={t("live.maxLine")} value={`${fmt(s.max_line_loading_percent, 1)} %`} color={loadingColor(s.max_line_loading_percent)} />
              <Stat label={t("live.maxTrafo")} value={s.max_trafo_loading_percent != null ? `${fmt(s.max_trafo_loading_percent, 1)} %` : t("live.na")} color={loadingColor(s.max_trafo_loading_percent)} />
              <Stat label={t("live.totalLoad")} value={`${fmt(s.total_load_mw * 1000, 1)} kW`} />
              <Stat label={t("live.generation")} value={`${fmt(s.total_gen_mw * 1000, 1)} kW`} />
              <Stat label={t("live.slack")} value={`${fmt(s.total_ext_grid_mw * 1000, 1)} kW`} />
              <Stat label={t("live.losses")} value={`${fmt(s.total_losses_mw * 1000, 2)} kW`} />
            </>
          ) : (
            // observed only: aggregates over placed meters
            <>
              <Stat label={t("live.measuredVmm")}
                    value={os?.vm_pu_min != null && os?.vm_pu_max != null ? `${fmt(os.vm_pu_min * V_BASE, 1)} / ${fmt(os.vm_pu_max * V_BASE, 1)} V` : t("live.na")} />
              <Stat label={t("live.measuredTrafo")}
                    value={os?.max_trafo_loading_percent != null ? `${fmt(os.max_trafo_loading_percent, 1)} %` : t("live.na")}
                    color={loadingColor(os?.max_trafo_loading_percent)} />
              <Stat label={t("live.measuredLoad")}
                    value={os?.measured_node_p_mw != null ? `${fmt(os.measured_node_p_mw * 1000, 1)} kW` : t("live.na")} />
              <Stat label={t("live.coverage")}
                    value={os ? `${os.n_node_meter}/${os.n_bus} · ${os.n_trafo_meter}/${os.n_trafo}` : "—"} />
            </>
          )}
          <Stat label={t("live.solveTime")} value={latest ? `${fmt(latest.solve_ms, 1)} ms` : "—"} />
          {!reveal && (
            <div className="muted" style={{ fontSize: "0.68rem", marginTop: 4 }}>
              {t("live.observedNote")}{!canReveal ? ` ${t("live.truthHidden")}` : ""}
            </div>
          )}
        </Section>

        {placement && (
          <Section title={t("meas.heading")} open={measOpen} onToggle={() => setMeasOpen((v) => !v)}
                   badges={[`📟 ${placement.coverage.n_node_meter + placement.coverage.n_trafo_meter}`]}>
            <MeasurementPanel placement={placement} onPreset={meterPreset} onMode={meterMode} />
          </Section>
        )}

        {sections.map((sec) => {
          const key = `${sec.kind}${sec.id}`;
          const name = elemName(sec.kind, sec.id);
          const bat = batteryAt(sec.kind, sec.id);
          const ctrl = controllerAt(sec.kind, sec.id);
          const liveCtrl = ctrl ? latest?.controllers?.find((c) => c.id === ctrl.id) ?? ctrl : undefined;
          const metered = meteredElem(sec.kind, sec.id);
          const nm = sec.kind === "bus" ? liveNodeMeas.get(sec.id) : undefined;
          const tm = sec.kind === "trafo" ? liveTrafoMeas.get(sec.id) : undefined;
          const live = bat ? batLive[bat.index] : undefined;
          return (
            <Section key={key} title={elemTitle(sec.kind, sec.id)} open={sec.open}
                     badges={[...(bat ? ["🔋"] : []), ...(ctrl ? ["🎛️"] : []),
                              ...(metered ? ["📟"] : []),
                              ...(sec.kind === "bus" && evBuses.includes(sec.id) ? ["🔌"] : []),
                              ...(sec.kind === "bus" && pvBuses.includes(sec.id) ? ["☀️"] : [])]}
                     onToggle={() => toggleOpen(sec.kind, sec.id)}
                     onClose={bat ? undefined : () => closeSection(sec.kind, sec.id)}>
              {sec.kind === "bus" && <NodeProfile embedded key={`np${derStamp}`} bus={sec.id} name={name} now={nowFrac} day={curDay} />}
              {sec.kind === "line" && <LineProfile embedded line={sec.id} name={name} now={nowFrac} day={curDay} />}
              {sec.kind === "trafo" && <TrafoProfile embedded trafo={sec.id} name={name} now={nowFrac} day={curDay} />}

              {sec.kind === "bus" && (evBuses.includes(sec.id) || pvBuses.includes(sec.id)) && (
                <DerPanel bus={sec.id} stamp={derStamp} onChanged={() => setDerStamp((v) => v + 1)} />
              )}

              {metered && (
                <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem" }}>
                    <span style={{ fontWeight: 600 }}>📟 {t("sec.readings")}</span>
                    <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                            onClick={() => removeMeterAt(sec.kind, sec.id)}>{t("menu.removeMeter")}</button>
                  </div>
                  <div className="muted" style={{ fontSize: "0.74rem", fontVariantNumeric: "tabular-nums" }}>
                    {sec.kind === "bus" && (nm
                      ? [nm.vm_pu != null ? t("tip.voltA", { v: fmt(nm.vm_pu * V_BASE, 1) }) : null,
                         nm.p_mw != null ? t("tip.meterP", { v: fmt(nm.p_mw * 1000, 1) }) : null,
                         nm.q_mvar != null ? t("tip.meterQ", { v: fmt(nm.q_mvar * 1000, 1) }) : null,
                         nm.i_ka != null ? t("tip.meterI", { v: fmt(nm.i_ka * 1000, 1) }) : null,
                        ].filter(Boolean).join(" · ")
                      : "—")}
                    {sec.kind === "trafo" && (tm
                      ? `${tm.loading_percent != null ? `${fmt(tm.loading_percent, 1)} %` : "—"}${tm.p_hv_mw != null ? ` · ${t("tip.meterP", { v: fmt(tm.p_hv_mw * 1000, 1) })}` : ""}`
                      : "—")}
                  </div>
                </div>
              )}

              {bat && (
                <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem", gap: 6 }}>
                    <span style={{ fontWeight: 600 }}>🔋</span>
                    <select value={bat.mode} style={{ flex: 1, fontSize: "0.72rem" }}
                            onChange={(e) => changeBatteryMode(bat.index, e.target.value as BatteryMode)}>
                      {batModes.filter((m) => m !== "price" || batHasPrices).map((m) => (
                        <option key={m} value={m}>{t(`bat.${m}`)}</option>
                      ))}
                    </select>
                    <BatterySize bat={bat} free={sec.kind === "trafo"}
                                 onSize={(c, p) => changeBatterySize(bat.index, c, p)} />
                  </div>
                  <Stat label={t("bat.soc")} value={`${fmt(live?.soc_percent ?? bat.soc_percent, 1)} %`} />
                  <Stat label={t("bat.pwr")} value={live ? `${fmt(live.p_mw * 1000, 2)} kW` : "—"} />
                  <BatteryProfile embedded key={`${bat.index}-${bat.mode}`} idx={bat.index} now={nowFrac} day={curDay} />
                  <button className="ghost" style={{ fontSize: "0.68rem", padding: "1px 6px", marginTop: 4 }}
                          onClick={() => removeBattery(bat.index)}>{t("menu.removeBattery")}</button>
                </div>
              )}

              {ctrl && liveCtrl && (
                <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem", gap: 6 }}>
                    <span style={{ fontWeight: 600 }}>🎛️ {t("ctrl.title")}</span>
                    <ControllerLimit ctrl={ctrl} onLimit={(p) => setControllerLimitAt(ctrl.id, p)} />
                    <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                            onClick={() => removeControllerById(ctrl.id)}>✕</button>
                  </div>
                  <Stat label={t("ctrl.evF")} value={`${Math.round(liveCtrl.ev_factor * 100)} %`}
                        color={liveCtrl.ev_factor < 1 ? "#f2ae00" : undefined} />
                  <Stat label={t("ctrl.pvF")} value={`${Math.round(liveCtrl.pv_factor * 100)} %`}
                        color={liveCtrl.pv_factor < 1 ? "#f2ae00" : undefined} />
                  <div className="muted" style={{ fontSize: "0.7rem", marginTop: 2 }}>
                    {liveCtrl.active ? `⚡ ${t("ctrl.active")}` : t("ctrl.idle")}
                  </div>
                </div>
              )}
            </Section>
          );
        })}

        <p className="muted" style={{ fontSize: "0.72rem", marginTop: "0.5rem" }}>
          {t("live.selectHint")}
        </p>

        {layout === "tree" && (
          <>
            <div className="legend">
              <span><i className="swatch" style={{ background: loadingColor(10) }} /> {t("live.legLow")}</span>
              <span><i className="swatch" style={{ background: loadingColor(65) }} /> {t("live.legMed")}</span>
              <span><i className="swatch" style={{ background: loadingColor(90) }} /> {t("live.legHigh")}</span>
              <span><i className="swatch" style={{ background: loadingColor(120) }} /> {t("live.legOver")}</span>
            </div>
            <div className="legend">
              <span><i className="swatch" style={{ background: voltageColor(0.9) }} /> {t("live.underV")}</span>
              <span><i className="swatch" style={{ background: voltageColor(1.0) }} /> {t("live.okV")}</span>
              <span><i className="swatch" style={{ background: voltageColor(1.1) }} /> {t("live.overV")}</span>
            </div>
          </>
        )}
        <p className="muted" style={{ fontSize: "0.72rem", marginTop: "0.6rem" }}>
          {layout === "map" ? t("live.mapHint") : t("live.schematicHint")}
        </p>
      </aside>

      <div className="controls-bar">
        <button className="primary" onClick={toggleRun}>
          {status?.running ? t("live.pause") : t("live.play")}
        </button>
        <span className="clock" style={{ minWidth: 180 }}>
          {t("live.time", { time: latest?.time_of_day ?? "00:00" })}
        </span>
        <input
          type="range"
          min={0}
          max={spd - 1}
          value={step}
          onChange={(e) => seek(+e.target.value)}
        />
        {nDays > 1 && (
          <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}
                 title={t("live.dayTitle")}>
            {t("live.dayLabel")}
            <input
              type="range"
              min={0}
              max={nDays - 1}
              value={dayIdx}
              onChange={(e) => seekDay(+e.target.value)}
              style={{ flex: "0 0 90px", width: 90 }}
            />
            <span style={{ minWidth: 74, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
              {pvDates[dayIdx] ?? `#${dayIdx + 1}`}
            </span>
          </label>
        )}
        <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6 }}
               title={t("live.stepDurTitle")}>
          {t("live.stepDur")}
          <input
            type="range"
            min={0.1}
            max={1}
            step={0.1}
            value={stepSeconds}
            onChange={(e) => changeInterval(+e.target.value)}
            style={{ flex: "0 0 90px", width: 90 }}
          />
          <span style={{ minWidth: 34, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
            {stepSeconds.toFixed(1).replace(".", ",")} s
          </span>
        </label>
      </div>
    </div>
  );
}

// classic home-storage units (kWh · kW); the busbar battery is sized freely
const HOME_SIZES: [number, number][] = [[5, 2.5], [10, 5], [15, 7.5], [20, 10]];
const numStyle: CSSProperties = {
  width: 64, fontSize: "0.72rem", background: "var(--panel-2)",
  color: "var(--text)", border: "1px solid var(--border)", borderRadius: 4,
  padding: "1px 4px",
};

function BatterySize({ bat, free, onSize }: {
  bat: Battery; free: boolean; onSize: (kwh: number, kw: number) => void;
}) {
  const { t } = useTranslation();
  const [kwh, setKwh] = useState(bat.capacity_kwh);
  const [kw, setKw] = useState(bat.power_kw);
  useEffect(() => { setKwh(bat.capacity_kwh); setKw(bat.power_kw); },
            [bat.capacity_kwh, bat.power_kw]);
  if (!free) {
    const cur = `${bat.capacity_kwh}|${bat.power_kw}`;
    const known = HOME_SIZES.some(([c, p]) => `${c}|${p}` === cur);
    return (
      <select value={cur} style={{ fontSize: "0.72rem" }}
              title={t("bat.sizeTitle")}
              onChange={(e) => { const [c, p] = e.target.value.split("|").map(Number); onSize(c, p); }}>
        {!known && <option value={cur}>{bat.capacity_kwh} kWh · {bat.power_kw} kW</option>}
        {HOME_SIZES.map(([c, p]) => (
          <option key={c} value={`${c}|${p}`}>{c} kWh · {p} kW</option>
        ))}
      </select>
    );
  }
  const dirty = kwh !== bat.capacity_kwh || kw !== bat.power_kw;
  const valid = kwh > 0 && kw > 0;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: "0.72rem" }}
          title={t("bat.sizeTitle")}>
      <input type="number" min={1} step={1} value={kwh} style={numStyle}
             onChange={(e) => setKwh(+e.target.value)} /> kWh
      <input type="number" min={1} step={1} value={kw} style={{ ...numStyle, width: 56 }}
             onChange={(e) => setKw(+e.target.value)} /> kW
      {dirty && (
        <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                disabled={!valid} onClick={() => onSize(kwh, kw)}>
          {t("bat.apply")}
        </button>
      )}
    </span>
  );
}

function ControllerLimit({ ctrl, onLimit }: {
  ctrl: GridController; onLimit: (pct: number) => void;
}) {
  const { t } = useTranslation();
  const [v, setV] = useState(ctrl.limit_pct);
  useEffect(() => setV(ctrl.limit_pct), [ctrl.limit_pct]);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 3, fontSize: "0.72rem" }}
          title={t("ctrl.limitTitle")}>
      {t("ctrl.limit")}
      <input type="number" min={20} max={150} step={5} value={v} style={{ ...numStyle, width: 52 }}
             onChange={(e) => setV(+e.target.value)}
             onBlur={() => v !== ctrl.limit_pct && v >= 20 && v <= 150 && onLimit(v)}
             onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} /> %
    </span>
  );
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="stat-row">
      <span className="muted">{label}</span>
      <span className="v" style={color ? { color } : undefined}>
        {value}
      </span>
    </div>
  );
}
