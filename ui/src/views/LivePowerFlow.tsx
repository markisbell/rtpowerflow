import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import type { Battery, BatteryMode, EngineStatus, MeasurementsResponse, MeterPreset, NodeMeasurement, Topology, TrafoMeasurement } from "../types";
import { useStepStream } from "../useWebSocket";
import { fmt, loadingColor, voltageColor } from "../scales";
import GridDiagram from "../components/GridDiagram";
import MapDiagram from "../components/MapDiagram";
import NodeProfile from "../components/NodeProfile";
import LineProfile from "../components/LineProfile";
import TrafoProfile from "../components/TrafoProfile";
import BatteryProfile from "../components/BatteryProfile";
import MeasurementPanel from "../components/MeasurementPanel";
import ElementMenu, { type MenuTarget } from "../components/ElementMenu";
import Section from "../components/Section";
import { gridDisplayName } from "../gridname";

type Layout = "map" | "tree";
type SelKind = "bus" | "line" | "trafo";
// One collapsible side-panel section per grid element. A section exists while
// pinned (via the element menu / ctrl-click) or while a battery sits at the
// element; its body (graphs, readings) renders lazily on expand.
interface Sec { kind: SelKind; id: number; open: boolean; }

export default function LivePowerFlow({ onActive }: { onActive: () => void }) {
  const { t } = useTranslation();
  const [topo, setTopo] = useState<Topology | null>(null);
  const [status, setStatus] = useState<EngineStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>("tree");
  const [showValues, setShowValues] = useState(false);
  const [sections, setSections] = useState<Sec[]>([]);
  const [menu, setMenu] = useState<MenuTarget | null>(null);
  const [ovOpen, setOvOpen] = useState(true);          // "Overview" section
  const [measOpen, setMeasOpen] = useState(false);     // bulk "Measurements" section
  const [stepSeconds, setStepSeconds] = useState(1);   // accelerated-tick interval (s/step)
  const [pvDates, setPvDates] = useState<string[]>([]); // real-PV day calendar (day slider)
  const [sideW, setSideW] = useState(340);             // resizable overview width (px)
  const [batteries, setBatteries] = useState<Battery[]>([]);
  const [batModes, setBatModes] = useState<BatteryMode[]>([]);
  const [batHasPrices, setBatHasPrices] = useState(false);
  const [placement, setPlacement] = useState<MeasurementsResponse | null>(null);  // meter placement
  const [gridId, setGridId] = useState<string | null>(null);   // active grid id -> localized name
  // Ground truth is shown by default so a fresh session sees a normal, colored
  // grid; the strict observed-only view (grey unmetered elements) is opt-in via
  // the eye toggle — otherwise the app boots into a near-invisible grid.
  const [revealTruth, setRevealTruth] = useState(true);
  const layoutInit = useRef(false);
  const intervalInit = useRef(false);

  const { latest, status: wsStatus } = useStepStream(true);

  const loadTopo = () => api.network().then(setTopo).catch((e) => setError(String(e)));
  const loadStatus = () => api.status().then(setStatus).catch(() => {});
  const reloadBatteries = () => api.batteries()
    .then((r) => { setBatteries(r.batteries); setBatModes(r.modes); setBatHasPrices(r.has_prices); })
    .catch(() => {});
  const reloadMeasurements = () => api.measurements().then(setPlacement).catch(() => {});

  useEffect(() => {
    loadTopo();
    loadStatus();
    onActive();
    api.pvDays().then((r) => setPvDates(r.dates)).catch(() => {});
    api.active().then((a) => setGridId(a.grid_id)).catch(() => {});
    reloadBatteries();
    reloadMeasurements();
    const t = setInterval(loadStatus, 2000);
    return () => clearInterval(t);
  }, []);

  // default to the real OSM map for grids that carry geo-coordinates, else schematic
  useEffect(() => {
    if (topo && !layoutInit.current) {
      layoutInit.current = true;
      setLayout(topo.has_geo ? "map" : "tree");
      setShowValues(topo.buses.length <= 40);   // on by default for small grids; toggle for big ones
    }
  }, [topo]);

  // drop stale sections when the grid changes; batteries + meters reset with it
  useEffect(() => { setSections([]); setMenu(null); reloadBatteries(); reloadMeasurements(); }, [topo?.name]);

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
      return add.length ? [...prev, ...add] : prev;
    });
  }, [batteries, topo]);

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
  const addBatteryAt = async (kind: SelKind, id: number, mode: BatteryMode) => {
    const bus = busForElement(kind, id);
    if (bus == null) return;
    try { await api.addBattery({ bus, capacity_kwh: 10, power_kw: 5, mode }); } finally { reloadBatteries(); }
    pinSection(kind, id);
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
  const reveal = revealTruth && canReveal;
  const liveNodeMeas = new Map<number, NodeMeasurement>();
  latest?.measurements?.nodes.forEach((n) => liveNodeMeas.set(n.bus, n));
  const liveTrafoMeas = new Map<number, TrafoMeasurement>();
  latest?.measurements?.trafos.forEach((tr) => liveTrafoMeas.set(tr.trafo, tr));
  const os = latest?.observed_summary;

  const meteredElem = (kind: SelKind, id: number) =>
    kind === "bus" ? meterBuses.includes(id) : kind === "trafo" ? meterTrafos.includes(id) : false;

  // menu context (only while open)
  const menuBattery = menu ? batteryAt(menu.kind, menu.id) : undefined;
  const menuMetered = menu ? meteredElem(menu.kind, menu.id) : false;

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
        <div className="layout-toggle">
          {topo.has_geo && (
            <button className={layout === "map" ? "on" : ""} onClick={() => setLayout("map")}
                    title={t("live.mapTitle")}>
              🗺 {t("live.map")}
            </button>
          )}
          <button className={layout === "tree" ? "on" : ""} onClick={() => setLayout("tree")}>
            {t("live.schematic")}
          </button>
          {layout === "tree" && (
            <button
              className={showValues ? "on" : ""}
              style={{ marginLeft: 6 }}
              onClick={() => setShowValues((v) => !v)}
              title={t("live.valuesTitle")}
            >
              {t("live.values")}
            </button>
          )}
          {canReveal && (
            <button
              className={revealTruth ? "on" : ""}
              style={{ marginLeft: 6 }}
              onClick={() => setRevealTruth((v) => !v)}
              title={t("live.revealTitle")}
            >
              👁 {revealTruth ? t("live.reveal") : t("live.revealOff")}
            </button>
          )}
        </div>
        {layout === "map" ? (
          <MapDiagram topo={topo} latest={latest} batteryBuses={batteryBuses} onSelectBus={selectBus}
                      onSelectLine={selectLine} onSelectTrafo={selectTrafo}
                      meterBuses={meterBuses} meterTrafos={meterTrafos} revealTruth={reveal} />
        ) : (
          <GridDiagram topo={topo} latest={latest} showValues={showValues} batteryBuses={batteryBuses}
                       onSelectBus={selectBus} selectedBuses={selBuses}
                       onSelectLine={selectLine} selectedLines={selLines}
                       onSelectTrafo={selectTrafo} selectedTrafos={selTrafos}
                       meterBuses={meterBuses} meterTrafos={meterTrafos} revealTruth={reveal} />
        )}
      </div>

      {menu && (
        <ElementMenu
          target={menu}
          hasBattery={!!menuBattery}
          hasMeter={menuMetered}
          modes={batModes.filter((m) => m !== "price" || batHasPrices)}
          onGraph={() => pinSection(menu.kind, menu.id)}
          onAddBattery={(mode) => addBatteryAt(menu.kind, menu.id, mode)}
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
          {reveal && s ? (
            // ground truth (revealed): the true system-wide summary
            <>
              <div className="muted" style={{ fontSize: "0.68rem", textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 2 }}>
                👁 {t("live.groundTruth")}
              </div>
              <Stat label={t("live.vminmax")} value={`${fmt(s.vm_pu_min, 3)} / ${fmt(s.vm_pu_max, 3)} pu`} />
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
                    value={os?.vm_pu_min != null ? `${fmt(os.vm_pu_min, 3)} / ${fmt(os.vm_pu_max, 3)} pu` : t("live.na")} />
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
            <MeasurementPanel placement={placement} onPreset={meterPreset} />
          </Section>
        )}

        {sections.map((sec) => {
          const key = `${sec.kind}${sec.id}`;
          const name = elemName(sec.kind, sec.id);
          const bat = batteryAt(sec.kind, sec.id);
          const metered = meteredElem(sec.kind, sec.id);
          const nm = sec.kind === "bus" ? liveNodeMeas.get(sec.id) : undefined;
          const tm = sec.kind === "trafo" ? liveTrafoMeas.get(sec.id) : undefined;
          const live = bat ? batLive[bat.index] : undefined;
          return (
            <Section key={key} title={elemTitle(sec.kind, sec.id)} open={sec.open}
                     badges={[...(bat ? ["🔋"] : []), ...(metered ? ["📟"] : [])]}
                     onToggle={() => toggleOpen(sec.kind, sec.id)}
                     onClose={bat ? undefined : () => closeSection(sec.kind, sec.id)}>
              {sec.kind === "bus" && <NodeProfile embedded bus={sec.id} name={name} now={nowFrac} day={curDay} />}
              {sec.kind === "line" && <LineProfile embedded line={sec.id} name={name} now={nowFrac} day={curDay} />}
              {sec.kind === "trafo" && <TrafoProfile embedded trafo={sec.id} name={name} now={nowFrac} day={curDay} />}

              {metered && (
                <div style={{ marginTop: 6, borderTop: "1px solid var(--border)", paddingTop: 5 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem" }}>
                    <span style={{ fontWeight: 600 }}>📟 {t("sec.readings")}</span>
                    <button className="ghost" style={{ fontSize: "0.68rem", padding: "0 6px" }}
                            onClick={() => removeMeterAt(sec.kind, sec.id)}>{t("menu.removeMeter")}</button>
                  </div>
                  <div className="muted" style={{ fontSize: "0.74rem", fontVariantNumeric: "tabular-nums" }}>
                    {sec.kind === "bus" && (nm
                      ? [nm.vm_pu != null ? t("tip.voltA", { v: fmt(nm.vm_pu, 3) }) : null,
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
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", fontSize: "0.75rem" }}>
                    <span style={{ fontWeight: 600 }}>🔋 {t(`bat.${bat.mode}`)}</span>
                    <span className="muted" style={{ fontVariantNumeric: "tabular-nums" }}>
                      {bat.capacity_kwh} kWh · {bat.power_kw} kW
                    </span>
                  </div>
                  <Stat label={t("bat.soc")} value={`${fmt(live?.soc_percent ?? bat.soc_percent, 1)} %`} />
                  <Stat label={t("bat.pwr")} value={live ? `${fmt(live.p_mw * 1000, 2)} kW` : "—"} />
                  <BatteryProfile embedded idx={bat.index} now={nowFrac} day={curDay} />
                  <button className="ghost" style={{ fontSize: "0.68rem", padding: "1px 6px", marginTop: 4 }}
                          onClick={() => removeBattery(bat.index)}>{t("menu.removeBattery")}</button>
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
