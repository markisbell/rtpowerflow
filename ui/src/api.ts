import type {
  ActiveGrid,
  ArchetypesResponse,
  AssignResponse,
  BatteriesResponse,
  Battery,
  BatteryMode,
  BatteryProfiles,
  EngineStatus,
  GridPreview,
  GridsResponse,
  LineProfiles,
  LoadgenPolicy,
  MeasurementsResponse,
  MeterMode,
  NodeDer,
  Scenario,
  MeterPreset,
  NodeProfiles,
  PvDays,
  StepResult,
  Topology,
  TrafoProfiles,
} from "./types";

// All backend calls go through "/api" (Vite dev proxy / nginx in prod).
const API = "/api";

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`GET ${path} -> ${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

async function post<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`POST ${path} -> ${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

async function del<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`DELETE ${path} -> ${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

export interface EstimationConfig {
  pv_pseudo: boolean;
  ev_pseudo: boolean;
  load_basis: "profile" | "slp";
  slp_annual_kwh: number;
  pseudo_std_pct: number;
  zero_injection: boolean;
}

export const api = {
  estConfig: () => get<EstimationConfig>("/estimation/config"),
  setEstConfig: (cfg: EstimationConfig) => post<EstimationConfig>("/estimation/config", cfg),
  grids: () => get<GridsResponse>("/grids"),
  gridPreview: (id: string) => get<GridPreview>(`/grids/${encodeURIComponent(id)}`),
  thumbnailUrl: (id: string) => `${API}/grids/${encodeURIComponent(id)}/thumbnail`,

  archetypes: () => get<ArchetypesResponse>("/loadgen/archetypes"),
  assign: (grid_id: string, policy: LoadgenPolicy) =>
    post<AssignResponse>("/loadgen/assign", { grid_id, policy }),

  apply: (grid_id: string, loadgen?: LoadgenPolicy) =>
    post<{ status: EngineStatus; active: ActiveGrid; network: Topology }>(
      "/config/apply",
      { grid_id, loadgen: loadgen ?? null },
    ),
  active: () => get<ActiveGrid>("/config/active"),

  network: () => get<Topology>("/network"),
  status: () => get<EngineStatus>("/status"),
  state: () => get<StepResult>("/state"),
  nodeProfiles: (bus: number) => get<NodeProfiles>(`/node/${bus}/profiles`),
  lineProfiles: (line: number) => get<LineProfiles>(`/line/${line}/profiles`),
  trafoProfiles: (trafo: number) => get<TrafoProfiles>(`/trafo/${trafo}/profiles`),

  start: () => post<EngineStatus>("/control/start"),
  pause: () => post<EngineStatus>("/control/pause"),
  resume: () => post<EngineStatus>("/control/resume"),
  seek: (step: number) => post<EngineStatus>(`/control/seek?step=${step}`),
  seekDay: (day: number) => post<EngineStatus>(`/control/seekday?day=${day}`),
  stepInterval: (seconds: number) => post<EngineStatus>(`/control/interval?seconds=${seconds}`),
  pvDays: () => get<PvDays>("/pv/days"),
  batteries: () => get<BatteriesResponse>("/batteries"),
  addBattery: (body: { bus: number; capacity_kwh: number; power_kw: number; mode: BatteryMode }) =>
    post<Battery>("/battery", body),
  removeBattery: (idx: number) => del<{ removed: number }>(`/battery/${idx}`),
  batteryProfiles: (idx: number) => get<BatteryProfiles>(`/battery/${idx}/profiles`),

  // observability: measurement device placement
  measurements: () => get<MeasurementsResponse>("/measurements"),
  placeNodeMeter: (bus: number) => post<MeasurementsResponse>("/measurements/node", { bus }),
  removeNodeMeter: (bus: number) => del<MeasurementsResponse>(`/measurements/node/${bus}`),
  placeTrafoMeter: (trafo: number) => post<MeasurementsResponse>("/measurements/trafo", { trafo }),
  removeTrafoMeter: (trafo: number) => del<MeasurementsResponse>(`/measurements/trafo/${trafo}`),
  scenarios: () => get<{ scenarios: Scenario[] }>("/scenarios"),
  saveScenario: (name: string, description: string) =>
    post<{ id: string }>("/scenarios", { name, description }),
  loadScenario: (id: string) =>
    post<{ status: EngineStatus; active: ActiveGrid; network: Topology }>(`/scenarios/${id}/load`),
  deleteScenario: (id: string) => del<{ deleted: string }>(`/scenarios/${id}`),
  nodeDer: (bus: number) => get<NodeDer>(`/node/${bus}/der`),
  addPv: (bus: number, kwp: number) => post<NodeDer>(`/pv`, { bus, kwp }),
  setPv: (sgen: number, kwp: number) => post<NodeDer>(`/pv/${sgen}?kwp=${kwp}`),
  addEv: (bus: number) => post<NodeDer>(`/ev`, { bus }),
  setEv: (load: number, start_min: number, dur_min: number) =>
    post<NodeDer>(`/ev/${load}?start_min=${start_min}&dur_min=${dur_min}`),
  removePv: (sgen: number) => del<NodeDer>(`/pv/${sgen}`),
  removeEv: (load: number) => del<NodeDer>(`/ev/${load}`),
  setBatteryMode: (index: number, mode: BatteryMode) =>
    post<BatteriesResponse>(`/battery/${index}/mode?name=${mode}`),
  meterPreset: (name: MeterPreset) =>
    post<MeasurementsResponse>(`/measurements/preset?name=${name}`),
  meterMode: (name: MeterMode) =>
    post<MeasurementsResponse>(`/measurements/mode?name=${name}`),
};

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}
