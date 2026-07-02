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
  in_service?: boolean;                 // false = normally-open ring tie (drawn dashed)
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

// One smart-meter reading at a bus (three-phase sums; per-phase is balanced).
export interface NodeMeasurement {
  bus: number;
  name: string;
  vm_pu: number | null;
  v_ll_kv: number | null;
  p_mw: number | null;
  q_mvar: number | null;
  s_mva: number | null;
  i_ka: number | null;
}
export interface TrafoMeasurement {
  trafo: number;
  name: string;
  hv_bus: number;
  lv_bus: number;
  loading_percent: number | null;
  p_hv_mw: number | null;
  q_hv_mvar: number | null;
  i_hv_ka: number | null;
  pl_mw: number | null;
}
export interface Coverage {
  n_bus: number;
  n_node_meter: number;
  n_trafo: number;
  n_trafo_meter: number;
  node_fraction: number;
  trafo_fraction: number;
}
// The observed projection: only what placed measurement devices reveal.
export interface Measurements {
  nodes: NodeMeasurement[];
  trafos: TrafoMeasurement[];
  coverage: Coverage;
  phases?: number;
  balanced?: boolean;
}
export interface ObservedSummary extends Coverage {
  vm_pu_min: number | null;
  vm_pu_max: number | null;
  max_trafo_loading_percent: number | null;
  measured_node_p_mw: number | null;
}

export interface StepResult {
  step: number;
  day: number;
  time_of_day: string;
  converged: boolean;
  solve_ms: number;
  timestamp: number;
  // The observed projection is ALWAYS present. The ground-truth fields below are
  // present only when the server exposes them (NETZSIM_EXPOSE_GROUND_TRUTH).
  measurements: Measurements;
  observed_summary: ObservedSummary | null;
  buses?: { index: number; name: string; vm_pu: number; va_degree: number; p_mw: number; q_mvar: number }[];
  lines?: { index: number; name: string; from_bus: number; to_bus: number; loading_percent: number; i_ka: number; p_from_mw: number; pl_mw: number }[];
  trafos?: { index: number; name: string; hv_bus: number; lv_bus: number; loading_percent: number; p_hv_mw: number; q_hv_mvar: number; i_hv_ka: number; pl_mw: number }[];
  ext_grids?: { index: number; name: string; p_mw: number; q_mvar: number }[];
  batteries: { index: number; bus: number; name: string; mode: BatteryMode; soc_percent: number; p_mw: number; capacity_kwh: number; power_kw: number }[];
  summary?: StepSummary;
  error: string | null;
}

export type MeterPreset = "all_nodes" | "all_trafos" | "substation_trafos" | "clear";
export interface MeasurementsResponse {
  node_buses: number[];
  trafo_idxs: number[];
  coverage: Coverage;
  presets: MeterPreset[];
  expose_ground_truth: boolean;
}

export type BatteryMode = "self" | "peak" | "price";
export interface Battery {
  index: number;
  bus: number;
  name: string;
  mode: BatteryMode;
  capacity_kwh: number;
  power_kw: number;
  soc_percent: number;
}
export interface BatteriesResponse {
  modes: BatteryMode[];
  has_prices: boolean;
  batteries: Battery[];
}
export interface BatteryProfiles {
  index: number;
  bus: number;
  name: string;
  mode: BatteryMode;
  steps_per_day: number;
  capacity_kwh: number;
  power_kw: number;
  soc: (number | null)[];
  power: (number | null)[];
  price: (number | null)[];
  price_lo: number | null;
  price_hi: number | null;
}

export type NodeSeriesKind = "residential" | "ev" | "pv";
export interface NodeProfiles {
  bus: number;
  name: string;
  steps_per_day: number;
  series: { kind: NodeSeriesKind; p_mw: (number | null)[] }[];
  voltage: (number | null)[];
}
export interface LineProfiles {
  line: number;
  name: string;
  from_bus: number;
  to_bus: number;
  steps_per_day: number;
  rated_i_ka: number | null;
  current: (number | null)[];
  loading: (number | null)[];
}
export interface TrafoProfiles {
  trafo: number;
  name: string;
  hv_bus: number;
  lv_bus: number;
  steps_per_day: number;
  sn_mva: number | null;
  power: (number | null)[];
  loading: (number | null)[];
}

export interface EngineStatus {
  running: boolean;
  step: number;
  day: number;
  steps_per_day: number;
  interval_seconds: number;
  n_days: number;
}

export interface PvDays {
  available: boolean;
  peak_w: number;
  dates: string[];
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
