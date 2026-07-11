import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Battery, BatteryMode, EngineStatus, GridController, NodeMeasurement, Topology, TrafoMeasurement } from "../types";
import { useStepStream } from "../useWebSocket";
import { useDerState, useEquipment, useExtNodes, useMeterPlacement, type SelKind } from "../hooks";
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
import ExtHistoryGraph from "../components/ExtHistoryGraph";
import Section from "../components/Section";
import OverviewSection from "../components/OverviewSection";
import AmpelSection from "../components/AmpelSection";
import CellsSection from "../components/CellsSection";
import { BatterySize, ControllerLimit, RontTarget, Stat } from "../components/EquipmentControls";
import { gridDisplayName } from "../gridname";
import type { LiveView } from "../App";
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
  const [ampelOpen, setAmpelOpen] = useState(true);    // Netzampel (coordinator)
  const [cellsOpen, setCellsOpen] = useState(false);   // ONS cell table
  const [focusCell, setFocusCell] = useState<string | null>(null); // map drill-down
  const [stepSeconds, setStepSeconds] = useState(1);   // accelerated-tick interval (s/step)
  const [pvDates, setPvDates] = useState<string[]>([]); // real-PV day calendar (day slider)
  const [sideW, setSideW] = useState(340);             // resizable overview width (px)
  const [gridId, setGridId] = useState<string | null>(null);   // active grid id -> localized name
  // Three parallel views of the grid: ground truth (default, so a fresh session
  // sees a normal colored grid), the strict observed-only projection, and the
  // WLS state estimate computed from the placed meters (estimator.py).
  const layoutInit = useRef(false);
  const intervalInit = useRef(false);

  const { latest, status: wsStatus } = useStepStream(true);

  // runtime state slices (hooks.ts): equipment CRUD, meter placement + TAF
  // raster stamp, per-node DER state — the view adds section pinning on top
  const {
    batteries, batModes, batHasPrices, controllers, ronts,
    reloadAll: reloadEquipment,
    addBattery, changeBatteryMode, changeBatterySize, removeBattery,
    addController, removeController: removeControllerById, setControllerLimit: setControllerLimitAt,
    addRont, removeRont: removeRontById, setRontTarget: setRontTargetAt,
  } = useEquipment();
  const {
    placement, meterStamp, reload: reloadMeasurements,
    placeMeter, removeMeter: removeMeterAt,
    preset: meterPreset, setMode: meterMode,
    modeAt: meterModeAt, setModeAt: setMeterModeAt,
  } = useMeterPlacement(measStamp);
  const {
    evBuses, pvBuses, derStamp, bump: bumpDer,
    addPv, addEv, removePv: removePvAt, removeEv: removeEvAt,
  } = useDerState(topo);
  const { extNodes, reloadExt, addExtNode, removeExtNode } = useExtNodes();

  const loadTopo = () => api.network().then(setTopo).catch((e) => setError(String(e)));
  const loadStatus = () => api.status().then(setStatus).catch(() => {});

  useEffect(() => {
    loadTopo();
    loadStatus();
    onActive();
    api.pvDays().then((r) => setPvDates(r.dates)).catch(() => {});
    api.active().then((a) => setGridId(a.grid_id)).catch(() => {});
    reloadEquipment();
    reloadExt();
    const t = setInterval(loadStatus, 2000);
    return () => clearInterval(t);
  }, []);

  // default to the real OSM map for grids that carry geo-coordinates, else schematic
  useEffect(() => {
    if (topo && !layoutInit.current) {
      layoutInit.current = true;
      onView({ layout: topo.has_geo ? "map" : "tree",
               showValues: topo.buses.length <= 40 });  // values on by default for small grids
    }
  }, [topo]);

  // drop stale sections when the grid changes; equipment + meters reset with it
  useEffect(() => { setSections([]); setMenu(null); reloadEquipment(); reloadMeasurements(); reloadExt(); }, [topo?.name]);

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
  // vertical structure: ONS cells — a Steuerbox (cell controller) is placed
  // at a cell's station (busbar of a spliced cell / MV bus of a lumped one),
  // the Netzampel coordinator at the slack/UW bus
  const cells = topo?.cells ?? [];
  const cellAtBus = (id: number) =>
    cells.find((c) => (c.lumped ? c.mv_bus === id : c.lv_busbar === id));
  const cellAtTrafo = (id: number) => cells.find((c) => c.station_trafos.includes(id));
  const extBuses = (topo?.ext_grids ?? []).map((e) => e.bus);
  const cellBusOf = (c: GridController): number => {
    const cc = cells.find((x) => x.id === c.cell);
    return cc ? (cc.lumped ? cc.mv_bus ?? -1 : cc.lv_busbar ?? -1) : -1;
  };

  // a station controller lives on the trafo, a bus controller on its node;
  // vertical: the cell controller on its station, the coordinator on the UW
  const controllerAt = (kind: SelKind, id: number): GridController | undefined => {
    if (kind === "trafo") {
      const cc = cellAtTrafo(id);
      return (cc && controllers.find((c) => c.scope === "cell" && c.cell === cc.id))
        || controllers.find((c) => c.scope === "station");
    }
    if (kind === "bus") {
      const own = controllers.find((c) => c.scope === "bus" && c.bus === id);
      if (own) return own;
      const cc = cellAtBus(id);
      if (cc) return controllers.find((c) => c.scope === "cell" && c.cell === cc.id);
      if (extBuses.includes(id)) return controllers.find((c) => c.scope === "mv");
      return undefined;
    }
    return undefined;
  };

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
  // exists for every battery not already covered (e.g. after a reload).
  // External nodes behave the same — their section is the live-value display.
  useEffect(() => {
    if (!topo) return;
    setSections((prev) => {
      const covered = (bus: number) => prev.some((s) =>
        (s.kind === "bus" && s.id === bus) ||
        (s.kind === "trafo" && topo.trafos.find((tr) => tr.id === s.id)?.lv_bus === bus));
      const keepBuses = [...new Set([...batteries.map((b) => b.bus),
                                     ...extNodes.map((x) => x.bus)])];
      const add = keepBuses.filter((b) => !covered(b))
        .map((b) => ({ kind: "bus" as const, id: b, open: false }));
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
  }, [batteries, controllers, extNodes, topo]);

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

  // ---- equipment actions: hook round-trips + section pinning --------------- //
  const addBatteryAt = async (kind: SelKind, id: number) => {
    const bus = busForElement(kind, id);
    if (bus == null) return;
    await addBattery(bus);
    pinSection(kind, id);
  };
  const addControllerAt = async (kind: SelKind, id: number) => {
    // vertical dispatch: cell station -> Steuerbox, UW/slack -> coordinator
    if (kind === "trafo") {
      const cc = cellAtTrafo(id);
      await addController(cc ? { scope: "cell", cell: cc.id } : { scope: "station" });
    } else if (kind === "bus") {
      const cc = cellAtBus(id);
      if (cc) await addController({ scope: "cell", cell: cc.id });
      else if (extBuses.includes(id) && cells.length) await addController({ scope: "mv" });
      else await addController({ scope: "bus", bus: id });
    } else return;
    pinSection(kind, id);
  };
  const addRontAt = async (trafo: number) => {
    await addRont(trafo);
    pinSection("trafo", trafo);
  };
  const addPvAt = async (bus: number) => {
    await addPv(bus);
    pinSection("bus", bus);
  };
  const addEvAt = async (bus: number) => {
    await addEv(bus);
    pinSection("bus", bus);
  };
  const placeMeterAt = async (kind: SelKind, id: number) => {
    await placeMeter(kind, id);
    pinSection(kind, id);
  };
  const addExtNodeAt = async (bus: number) => {
    await addExtNode(bus);
    pinSection("bus", bus);
  };

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
  // external nodes: placement (badges/menu) + the per-step live values
  const extFeedBuses = extNodes.map((x) => x.bus);
  const extNodeAt = (bus: number) => extNodes.find((x) => x.bus === bus);

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
  // the day graphs load only the layers this perspective may see (the backend
  // enforces it): Lastfluss = truth curves, Gemessen = the meters' own
  // readings in the metering raster, Schätzung = all layers overlaid
  const profileView = mode === "observed" ? "measured" as const : mode;
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

  // 🎛 badge positions: bus controllers at their node, cell controllers at
  // their station, the coordinator at the UW, the station controller at the
  // LV busbar of the (first) transformer
  const controllerBuses = controllers
    .map((c) => c.scope === "bus" ? c.bus ?? -1
      : c.scope === "cell" ? cellBusOf(c)
      : c.scope === "mv" ? extBuses[0] ?? -1
      : topo?.trafos[0]?.lv_bus ?? -1)
    .filter((b) => b >= 0);

  // 🚦 stations whose Steuerbox is currently dimming (live factors from the
  // last frame) — the map marks them with a signal ring
  const liveCtrls = latest?.controllers ?? controllers;
  const signalBuses = liveCtrls
    .filter((c) => c.scope === "cell" && (c.ev_factor < 1 || c.pv_factor < 1))
    .map((c) => cellBusOf(c))
    .filter((b) => b >= 0);
  const coordLive = liveCtrls.find((c) => c.scope === "mv")
    ?? controllers.find((c) => c.scope === "mv");
  const cellCtrlCount = controllers.filter((c) => c.scope === "cell").length;

  // drill-down: the focused cell's buses drive the map's fitBounds
  const focused = focusCell ? cells.find((c) => c.id === focusCell) : undefined;
  const focusBuses = focused
    ? (focused.lumped ? (focused.mv_bus != null ? [focused.mv_bus] : [])
                      : focused.buses)
    : [];
  const openCell = (c: (typeof cells)[number]) => {
    setFocusCell(c.id);
    if (!c.lumped && c.station_trafos.length) pinSection("trafo", c.station_trafos[0]);
    else if (c.mv_bus != null) pinSection("bus", c.mv_bus);
  };

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
                      controllerBuses={controllerBuses} signalBuses={signalBuses}
                      focusBuses={focusBuses}
                      selectedBuses={selBuses} selectedTrafos={selTrafos}
                      onSelectLine={selectLine} onSelectTrafo={selectTrafo}
                      evBuses={evBuses} pvBuses={pvBuses} extFeedBuses={extFeedBuses}
                      meterBuses={meterBuses} meterTrafos={meterTrafos} revealTruth={mode !== "observed"} />
        ) : (
          <GridDiagram topo={topo} latest={frame} showValues={showValues} batteryBuses={batteryBuses}
                       controllerBuses={controllerBuses}
                       onSelectBus={selectBus} selectedBuses={selBuses}
                       onSelectLine={selectLine} selectedLines={selLines}
                       onSelectTrafo={selectTrafo} selectedTrafos={selTrafos}
                       evBuses={evBuses} pvBuses={pvBuses} extFeedBuses={extFeedBuses}
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
          controllerLabel={menu.kind === "trafo" && cellAtTrafo(menu.id) ? t("menu.addControllerCell")
            : menu.kind === "bus" && cellAtBus(menu.id) ? t("menu.addControllerCell")
            : menu.kind === "bus" && extBuses.includes(menu.id) && cells.length ? t("menu.addControllerMv")
            : undefined}
          onAddController={() => addControllerAt(menu.kind, menu.id)}
          onRemoveController={() => menuController && removeControllerById(menuController.id)}
          hasRont={menu.kind === "trafo" && ronts.some((r) => r.trafo === menu.id)}
          onAddRont={menu.kind === "trafo" ? () => addRontAt(menu.id) : undefined}
          onRemoveRont={() => { const r = ronts.find((x) => x.trafo === menu.id); if (r) removeRontById(r.id); }}
          hasExt={menu.kind === "bus" && !!extNodeAt(menu.id)}
          onAddExt={menu.kind === "bus" ? () => addExtNodeAt(menu.id) : undefined}
          onRemoveExt={() => { const x = extNodeAt(menu.id); if (x) removeExtNode(x.id); }}
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

        <OverviewSection open={ovOpen} onToggle={() => setOvOpen((v) => !v)}
                         mode={mode} est={est} summary={s} observed={os}
                         solveMs={latest?.solve_ms ?? null} canReveal={canReveal} />

        {placement && (
          <Section title={t("meas.heading")} open={measOpen} onToggle={() => setMeasOpen((v) => !v)}
                   badges={[`📟 ${placement.coverage.n_node_meter + placement.coverage.n_trafo_meter}`]}>
            <MeasurementPanel placement={placement} onPreset={meterPreset} onMode={meterMode} />
          </Section>
        )}

        {(coordLive || cellCtrlCount > 0) && (
          <AmpelSection open={ampelOpen} onToggle={() => setAmpelOpen((v) => !v)}
                        coordinator={coordLive} nCells={cells.length}
                        nBoxes={cellCtrlCount} nDimming={signalBuses.length}
                        onLimit={setControllerLimitAt} onRemove={removeControllerById} />
        )}

        {cells.length > 0 && (
          <CellsSection open={cellsOpen} onToggle={() => setCellsOpen((v) => !v)}
                        cells={cells}
                        nodeMeas={latest?.measurements?.nodes ?? []}
                        trafoMeas={latest?.measurements?.trafos ?? []}
                        signalBuses={signalBuses}
                        boxCells={controllers.filter((c) => c.scope === "cell").map((c) => c.cell)}
                        meterBuses={meterBuses} meterTrafos={meterTrafos}
                        focusCell={focusCell} onBack={() => setFocusCell(null)}
                        onOpen={openCell} />
        )}

        {sections.map((sec) => {
          const key = `${sec.kind}${sec.id}`;
          const name = elemName(sec.kind, sec.id);
          const bat = batteryAt(sec.kind, sec.id);
          const ctrl = controllerAt(sec.kind, sec.id);
          const liveCtrl = ctrl ? latest?.controllers?.find((c) => c.id === ctrl.id) ?? ctrl : undefined;
          const ront = sec.kind === "trafo" ? ronts.find((r) => r.trafo === sec.id) : undefined;
          const liveRont = ront ? latest?.ronts?.find((r) => r.id === ront.id) ?? ront : undefined;
          const metered = meteredElem(sec.kind, sec.id);
          const nm = sec.kind === "bus" ? liveNodeMeas.get(sec.id) : undefined;
          const tm = sec.kind === "trafo" ? liveTrafoMeas.get(sec.id) : undefined;
          const live = bat ? batLive[bat.index] : undefined;
          const extN = sec.kind === "bus" ? extNodeAt(sec.id) : undefined;
          const liveX = extN ? latest?.ext_nodes?.find((x) => x.id === extN.id) ?? extN : undefined;
          return (
            <Section key={key} title={elemTitle(sec.kind, sec.id)} open={sec.open}
                     badges={[...(bat ? ["🔋"] : []), ...(ctrl ? ["🎛️"] : []),
                              ...(metered ? ["📟"] : []), ...(extN ? ["📡"] : []),
                              ...(sec.kind === "bus" && evBuses.includes(sec.id) ? ["🔌"] : []),
                              ...(sec.kind === "bus" && pvBuses.includes(sec.id) ? ["☀️"] : [])]}
                     onToggle={() => toggleOpen(sec.kind, sec.id)}
                     onClose={bat || extN ? undefined : () => closeSection(sec.kind, sec.id)}>
              {sec.kind === "bus" && <NodeProfile embedded key={`np${derStamp}`} bus={sec.id} name={name} now={nowFrac} day={curDay} view={profileView} stamp={measStamp + meterStamp} />}
              {sec.kind === "line" && <LineProfile embedded line={sec.id} name={name} now={nowFrac} day={curDay} view={profileView} stamp={measStamp + meterStamp} />}
              {sec.kind === "trafo" && <TrafoProfile embedded trafo={sec.id} name={name} now={nowFrac} day={curDay} view={profileView} stamp={measStamp + meterStamp} />}

              {sec.kind === "bus" && (evBuses.includes(sec.id) || pvBuses.includes(sec.id)) && (
                <DerPanel bus={sec.id} stamp={derStamp} onChanged={bumpDer} />
              )}

              {metered && (
                <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem", gap: 6 }}>
                    <span style={{ fontWeight: 600 }}>📟 {t("sec.readings")}</span>
                    <span className="mbar-seg mini" title={t("meas.deviceModeTitle")}>
                      <button className={meterModeAt(sec.kind, sec.id) === "full" ? "on" : ""}
                              onClick={() => setMeterModeAt(sec.kind, sec.id, "full")}>
                        {t("meas.modeFull")}
                      </button>
                      <button className={meterModeAt(sec.kind, sec.id) === "standard" ? "on" : ""}
                              onClick={() => setMeterModeAt(sec.kind, sec.id, "standard")}>
                        {t("meas.modeStd")}
                      </button>
                    </span>
                    <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                            onClick={() => removeMeterAt(sec.kind, sec.id)}>{t("menu.removeMeter")}</button>
                  </div>
                  <div className="muted" style={{ fontSize: "0.74rem", fontVariantNumeric: "tabular-nums" }}>
                    {sec.kind === "bus" && (nm
                      ? ([nm.vm_pu != null ? t("tip.voltA", { v: fmt(nm.vm_pu * V_BASE, 1) }) : null,
                         nm.p_mw != null ? t("tip.meterP", { v: fmt(nm.p_mw * 1000, 1) }) : null,
                         nm.q_mvar != null ? t("tip.meterQ", { v: fmt(nm.q_mvar * 1000, 1) }) : null,
                         nm.i_ka != null ? t("tip.meterI", { v: fmt(nm.i_ka * 1000, 1) }) : null,
                        ].filter(Boolean).join(" · ") || "—")
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

              {ront && liveRont && (
                <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem", gap: 6 }}>
                    <span style={{ fontWeight: 600 }}>🔧 {t("ront.title")}</span>
                    <RontTarget ront={ront} onTarget={(v) => setRontTargetAt(ront.id, v)} />
                    <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                            onClick={() => removeRontById(ront.id)}>✕</button>
                  </div>
                  <Stat label={t("ront.tap")}
                        value={`${liveRont.tap_pos > 0 ? "+" : ""}${liveRont.tap_pos} / ±${liveRont.tap_max} (à ${fmt(liveRont.tap_step_percent, 1)} %)`}
                        color={liveRont.tap_pos !== 0 ? "#f2ae00" : undefined} />
                  <Stat label={t("ront.seenV")}
                        value={liveRont.seen_v != null
                          ? `${fmt(liveRont.seen_v * V_BASE, 1)} V (${t(liveRont.seen_src === "meter" ? "ctrl.srcMeter" : "ctrl.srcEst")})`
                          : `⚠️ ${t("ctrl.blind")}`} />
                  <div className="muted" style={{ fontSize: "0.7rem", marginTop: 2 }}>
                    {t("ront.band", { lo: fmt((liveRont.v_target - liveRont.deadband) * V_BASE, 1),
                                      hi: fmt((liveRont.v_target + liveRont.deadband) * V_BASE, 1) })}
                  </div>
                </div>
              )}

              {extN && liveX && (
                <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem", gap: 6 }}>
                    <span style={{ fontWeight: 600 }}>📡 {t("ext.title")}</span>
                    <span className="muted" style={{ flex: 1, fontSize: "0.68rem", textAlign: "right" }}>
                      ±{fmt(extN.p_max_kw, 0)} kW · {t(extN.on_timeout === "zero" ? "ext.zero" : "ext.hold")}
                    </span>
                    <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                            onClick={() => removeExtNode(extN.id)}>✕</button>
                  </div>
                  <Stat label={t("ext.applied")}
                        value={`${fmt(liveX.p_kw, 1)} kW${liveX.q_kvar ? ` · ${fmt(liveX.q_kvar, 1)} kvar` : ""}`}
                        color={liveX.stale ? "#f2ae00" : undefined} />
                  <Stat label={t("ext.age")}
                        value={liveX.age_s != null ? t("ext.ageVal", { s: fmt(liveX.age_s, 0) }) : t("ext.never")} />
                  {liveX.stale && (
                    <div className="muted" style={{ fontSize: "0.7rem", marginTop: 2, color: "#f2ae00" }}>
                      ⚠️ {liveX.age_s != null
                        ? t("ext.stale", { s: fmt(liveX.age_s, 0),
                                           policy: t(extN.on_timeout === "zero" ? "ext.policyZero" : "ext.policyHold") })
                        : t("ext.staleNever", { policy: t(extN.on_timeout === "zero" ? "ext.policyZero" : "ext.policyHold") })}
                    </div>
                  )}
                  <ExtHistoryGraph id={extN.id} now={nowFrac} />
                  <div className="muted" style={{ fontSize: "0.66rem", marginTop: 3 }}>
                    {t("ext.feedHint", { id: extN.id })}
                  </div>
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
                  <Stat label={t("ctrl.seen")}
                        value={liveCtrl.seen_pct != null
                          ? `${fmt(liveCtrl.seen_pct, 1)} % (${t(liveCtrl.seen_src === "meter" ? "ctrl.srcMeter" : "ctrl.srcEst")})`
                          : "—"}
                        color={liveCtrl.seen_pct != null && liveCtrl.seen_pct > liveCtrl.limit_pct ? "#e05c4a" : undefined} />
                  <Stat label={t("ctrl.evF")} value={`${Math.round(liveCtrl.ev_factor * 100)} %`}
                        color={liveCtrl.ev_factor < 1 ? "#f2ae00" : undefined} />
                  <Stat label={t("ctrl.pvF")} value={`${Math.round(liveCtrl.pv_factor * 100)} %`}
                        color={liveCtrl.pv_factor < 1 ? "#f2ae00" : undefined} />
                  <div className="muted" style={{ fontSize: "0.7rem", marginTop: 2 }}>
                    {liveCtrl.seen_pct == null ? `⚠️ ${t("ctrl.blind")}`
                      : liveCtrl.active ? `⚡ ${t("ctrl.active")}` : t("ctrl.idle")}
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
