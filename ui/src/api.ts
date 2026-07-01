import type {
  ActiveGrid,
  ArchetypesResponse,
  AssignResponse,
  EngineStatus,
  GridPreview,
  GridsResponse,
  LoadgenPolicy,
  NodeProfiles,
  StepResult,
  Topology,
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

  start: () => post<EngineStatus>("/control/start"),
  pause: () => post<EngineStatus>("/control/pause"),
  resume: () => post<EngineStatus>("/control/resume"),
  seek: (step: number) => post<EngineStatus>(`/control/seek?step=${step}`),
};

export function wsUrl(): string {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws`;
}
