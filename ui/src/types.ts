// Mirrors the netzsim REST/WebSocket payloads (see src/netzsim/api.py).

export interface GridListItem {
  id: string;
  name: string;
  category: string;
  thumbnail: string | null;
  voltage?: "MV" | "LV" | null;
  character?: "rural" | "suburban" | "urban" | null;
  nodes?: number | null;
  n_bus?: number;
  n_line?: number;
  n_trafo?: number;
  n_load?: number;
}

export interface GridsResponse {
  available: boolean;
  archive: string;
  grids: GridListItem[];
}

export interface GridPreview {
  name: string;
  n_bus: number;
  n_line: number;
  n_trafo: number;
  n_load: number;
  buses: { id: number; name: string; vn_kv: number; zone?: string }[];
  lines: { name: string | null; from_bus: number; to_bus: number; length_km: number }[];
  trafos: { name: string | null; hv_bus: number; lv_bus: number; sn_mva: number }[];
  notes: string[];
}

export interface Archetype {
  id: string;
  name: string;
  label: string;
  annual_kwh: number;
  mean_kw: number;
  peak_kw: number;
  n_variants: number;
}

export interface ArchetypesResponse {
  available: boolean;
  ev_available: boolean;
  steps: number;
  archetypes: Archetype[];
}

export interface LoadgenPolicy {
  archetypes?: string[] | null;
  mode?: "round_robin" | "random";
  seed?: number;
  scale?: number;
  power_factor?: number;
  jitter_minutes?: number;
  ev_penetration?: number;
  ev_charger_kw?: number;
  ev_daily_kwh?: number;
  pv_penetration?: number;
  pv_kwp?: number;
}

export interface AssignResponse {
  grid_id: string;
  steps: number;
  n_load: number;
  n_ev: number;
  n_pv: number;
  archetypes_used: string[];
  total_load_p_mw: number[];
  total_pv_p_mw: number[];
  net_p_mw: number[];
  peak_load_mw: number;
  peak_net_mw: number;
  min_net_mw: number;
  mean_load_mw: number;
  assignments: { name: string | null; bus: number; archetype: string; variant: number; ev: boolean }[];
}

export interface TopoBus {
  id: number;
  name: string;
  vn_kv: number;
  zone?: string;
  x: number;   // geographic/synthetic layout (normalized 0..1)
  y: number;
  tx: number;  // tidy-tree layout
  ty: number;
  geo: [number, number] | null; // real [lon, lat] (WGS84) for ding0 grids
}
export interface TopoLine {
  id: number;
  name: string | null;
  from_bus: number;
  to_bus: number;
  length_km: number;
  geometry?: [number, number][] | null; // [lon,lat] polyline (OSM-routed cables)
}
export interface TopoTrafo {
  id: number;
  name: string | null;
  hv_bus: number;
  lv_bus: number;
  sn_mva: number;
}

export interface Topology {
  name: string;
  f_hz: number;
  steps_per_day: number;
  has_geo: boolean;
  buses: TopoBus[];
  lines: TopoLine[];
  trafos: TopoTrafo[];
  ext_grids: { id: number; name: string; bus: number }[];
  load_buses: number[];
  sgen_buses: number[];
  cabinet_buses?: number[];
  n_load: number;
  n_sgen: number;
  n_trafo: number;
}

export interface StepSummary {
  n_bus: number;
  n_line: number;
  n_trafo: number;
  vm_pu_min: number;
  vm_pu_max: number;
  max_line_loading_percent: number | null;
  max_trafo_loading_percent: number | null;
  total_load_mw: number;
  total_gen_mw: number;
  total_ext_grid_mw: number;
  total_losses_mw: number;
}

export interface StepResult {
  step: number;
  day: number;
  time_of_day: string;
  converged: boolean;
  solve_ms: number;
  timestamp: number;
  buses: { index: number; name: string; vm_pu: number; va_degree: number; p_mw: number; q_mvar: number }[];
  lines: { index: number; name: string; from_bus: number; to_bus: number; loading_percent: number; i_ka: number; p_from_mw: number; pl_mw: number }[];
  trafos: { index: number; name: string; hv_bus: number; lv_bus: number; loading_percent: number; p_hv_mw: number; q_hv_mvar: number; i_hv_ka: number; pl_mw: number }[];
  ext_grids: { index: number; name: string; p_mw: number; q_mvar: number }[];
  summary: StepSummary;
  error: string | null;
}

export interface EngineStatus {
  running: boolean;
  step: number;
  day: number;
  steps_per_day: number;
  interval_seconds: number;
}

export interface ActiveGrid {
  grid_id: string | null;
  name: string;
  category: string | null;
  source: string;
  n_bus: number;
  n_line: number;
  n_trafo: number;
  n_load: number;
  notes: string[];
  load_source?: string;
  n_ev?: number;
  n_pv?: number;
}
