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

export const api = {
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
};

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}
