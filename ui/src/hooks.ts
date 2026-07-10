/**
 * State hooks for the Live view (extracted from LivePowerFlow, 2026-07-10).
 * Each hook owns one slice of runtime state + its API round-trips; the view
 * composes them and adds the presentation concerns (section pinning, menu).
 */
import { useEffect, useState } from "react";
import { api } from "./api";
import type {
  Battery,
  BatteryMode,
  GridController,
  MeasurementsResponse,
  MeterMode,
  MeterPreset,
  RontInfo,
  Topology,
} from "./types";

export type SelKind = "bus" | "line" | "trafo";

/** Batteries, overload controllers and rONTs: list state + CRUD round-trips.
 *  Mutations reload their slice (or adopt the response) so the UI converges
 *  on the server state even when a call fails. */
export function useEquipment() {
  const [batteries, setBatteries] = useState<Battery[]>([]);
  const [batModes, setBatModes] = useState<BatteryMode[]>([]);
  const [batHasPrices, setBatHasPrices] = useState(false);
  const [controllers, setControllers] = useState<GridController[]>([]);
  const [ronts, setRonts] = useState<RontInfo[]>([]);

  const reloadBatteries = () => api.batteries()
    .then((r) => { setBatteries(r.batteries); setBatModes(r.modes); setBatHasPrices(r.has_prices); })
    .catch(() => {});
  const reloadControllers = () => api.controllers()
    .then((r) => setControllers(r.controllers)).catch(() => {});
  const reloadRonts = () => api.ronts()
    .then((r) => setRonts(r.ronts)).catch(() => {});
  const reloadAll = () => { reloadBatteries(); reloadControllers(); reloadRonts(); };

  const addBattery = async (bus: number) => {
    // deploys with the default strategy; switched later in the node's section
    try { await api.addBattery({ bus, capacity_kwh: 10, power_kw: 5, mode: "self" }); } finally { reloadBatteries(); }
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

  const addController = async (req: { scope: GridController["scope"]; bus?: number; cell?: string }) => {
    try { await api.addController(req); } finally { reloadControllers(); }
  };
  const removeController = async (cid: number) => {
    try { await api.removeController(cid); } finally { reloadControllers(); }
  };
  const setControllerLimit = async (cid: number, pct: number) => {
    try {
      const r = await api.setControllerLimit(cid, pct);
      setControllers(r.controllers);
    } catch { reloadControllers(); }
  };

  const addRont = async (trafo: number) => {
    try { await api.addRont({ trafo }); } finally { reloadRonts(); }
  };
  const removeRont = async (rid: number) => {
    try { await api.removeRont(rid); } finally { reloadRonts(); }
  };
  const setRontTarget = async (rid: number, v_target: number) => {
    try { await api.setRont(rid, v_target); } finally { reloadRonts(); }
  };

  return {
    batteries, batModes, batHasPrices, controllers, ronts,
    reloadAll,
    addBattery, changeBatteryMode, changeBatterySize, removeBattery,
    addController, removeController, setControllerLimit,
    addRont, removeRont, setRontTarget,
  };
}

/** Meter placement + per-device TAF fidelity. `meterStamp` bumps on every
 *  change so the day graphs refetch in the new metering raster; `measStamp`
 *  (menu-bar actions in App) triggers a reload from outside. */
export function useMeterPlacement(measStamp: number) {
  const [placement, setPlacement] = useState<MeasurementsResponse | null>(null);
  const [meterStamp, setMeterStamp] = useState(0);

  const reload = () => api.measurements().then(setPlacement).catch(() => {});
  useEffect(() => { reload(); }, []);
  // meter changes made in the menu bar (presets, TAF mode) -> refresh placement
  useEffect(() => { if (measStamp > 0) reload(); }, [measStamp]);

  const bump = () => setMeterStamp((s) => s + 1);
  const placeMeter = async (kind: SelKind, id: number) => {
    if (kind === "bus") setPlacement(await api.placeNodeMeter(id));
    else if (kind === "trafo") setPlacement(await api.placeTrafoMeter(id));
    bump();
  };
  const removeMeter = async (kind: SelKind, id: number) => {
    if (kind === "bus") setPlacement(await api.removeNodeMeter(id));
    else if (kind === "trafo") setPlacement(await api.removeTrafoMeter(id));
    bump();
  };
  const preset = async (name: MeterPreset) => setPlacement(await api.meterPreset(name));
  const setMode = async (name: MeterMode) => setPlacement(await api.meterMode(name));
  // per-device TAF fidelity (the upsert POST also changes an existing meter)
  const modeAt = (kind: SelKind, id: number): MeterMode =>
    ((kind === "trafo" ? placement?.trafo_modes?.[String(id)]
                       : placement?.node_modes?.[String(id)])
     ?? placement?.mode ?? "full");
  const setModeAt = async (kind: SelKind, id: number, m: MeterMode) => {
    if (kind === "bus") setPlacement(await api.placeNodeMeter(id, m));
    else if (kind === "trafo") setPlacement(await api.placeTrafoMeter(id, m));
    bump();   // day graphs refetch in the device's new raster
  };

  return { placement, meterStamp, reload, placeMeter, removeMeter, preset, setMode, modeAt, setModeAt };
}

/** Which buses host EV charging / PV (seeded from the topology, extended
 *  live on add) + a stamp that refreshes graphs/panels after DER edits. */
export function useDerState(topo: Topology | null) {
  const [evBuses, setEvBuses] = useState<number[]>([]);
  const [pvBuses, setPvBuses] = useState<number[]>([]);
  const [derStamp, setDerStamp] = useState(0);

  useEffect(() => {
    setEvBuses(topo?.ev_buses ?? []);
    setPvBuses(topo?.pv_buses ?? []);
  }, [topo]);

  const bump = () => setDerStamp((v) => v + 1);
  const addPv = async (bus: number) => {
    await api.addPv(bus, 5);
    setPvBuses((prev) => (prev.includes(bus) ? prev : [...prev, bus]));
    bump();
  };
  const addEv = async (bus: number) => {
    await api.addEv(bus);
    setEvBuses((prev) => (prev.includes(bus) ? prev : [...prev, bus]));
    bump();
  };
  const removePv = async (bus: number) => {
    const der = await api.nodeDer(bus);
    if (der.pv) await api.removePv(der.pv.sgen);
    setPvBuses((prev) => prev.filter((b) => b !== bus));
    bump();
  };
  const removeEv = async (bus: number) => {
    const der = await api.nodeDer(bus);
    if (der.ev) await api.removeEv(der.ev.load);
    setEvBuses((prev) => prev.filter((b) => b !== bus));
    bump();
  };

  return { evBuses, pvBuses, derStamp, bump, addPv, addEv, removePv, removeEv };
}
