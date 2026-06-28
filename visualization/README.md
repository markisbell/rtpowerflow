# Visualization stack — collector + InfluxDB + Grafana

The second application. It reads the realtime power-flow service's **REST API**,
stores every solved step in **InfluxDB**, and visualizes it in **Grafana**.

```
 netzsim  ──GET /state──►  collector  ──write──►  InfluxDB  ──Flux query──►  Grafana
 (app 1)    (REST poll)    (app 2a)              (TSDB)                     (app 2b)
```

## Components

| Service     | Image / build              | Port | Role |
|-------------|----------------------------|------|------|
| `collector` | `./visualization/collector`| —    | Polls `GET /state`, dedupes by (day, step), writes points |
| `influxdb`  | `influxdb:2.7`             | 8086 | Time-series store, bucket `powerflow` |
| `grafana`   | `grafana/grafana:11.1.0`   | 3000 | Auto-provisioned datasource + dashboard |

## Data model (InfluxDB measurements)

| Measurement | Tags | Fields |
|-------------|------|--------|
| `summary`   | `time_of_day` | `converged`, `solve_ms`, `vm_pu_min/max`, `max_line_loading_percent`, `total_load_mw`, `total_gen_mw`, `total_ext_grid_mw`, `total_losses_mw`, `day`, `step`, … |
| `bus`       | `bus_index`, `bus_name`, `time_of_day` | `vm_pu`, `va_degree`, `p_mw`, `q_mvar` |
| `line`      | `line_index`, `line_name`, `time_of_day` | `loading_percent`, `i_ka`, `p_from_mw`, `pl_mw` |
| `ext_grid`  | `eg_index`, `eg_name`, `time_of_day` | `p_mw`, `q_mvar` |

Each point is timestamped with the **wall-clock time the step was solved**, so a
Grafana "last 5 minutes" view tracks the accelerated realtime simulation live.

## Run the whole system

```bash
docker compose up --build
```

Then open:
- **Grafana**  → <http://localhost:3000>  (login `admin` / `admin`) → dashboard *netzsim — Realtime Power Flow*
- **InfluxDB** → <http://localhost:8086>  (login `admin` / `netzsim-admin`)
- **netzsim**  → <http://localhost:8000>  (built-in monitor + REST/WS)

The dashboard auto-refreshes every 5 s and shows bus voltages, line loading
(with a 100 % threshold line), the power balance, max-loading gauge, voltage
min/max, solver time and convergence status.

## Configuration

Collector (env in `docker-compose.yml`):

| Var | Default | Meaning |
|-----|---------|---------|
| `NETZSIM_URL` | `http://netzsim:8000` | Power-flow REST base URL |
| `INFLUX_URL` | `http://influxdb:8086` | InfluxDB URL |
| `INFLUX_TOKEN` | `netzsim-dev-token` | Write token |
| `INFLUX_ORG` | `netzsim` | Org |
| `INFLUX_BUCKET` | `powerflow` | Target bucket |
| `POLL_INTERVAL_SECONDS` | `0.5` | REST poll period — keep ≤ the sim's step interval so no step is missed |

> **Security note:** the InfluxDB admin token and Grafana/InfluxDB passwords in
> `docker-compose.yml` are **development defaults**. Change them (and move them to
> a `.env` / secrets) before any non-local deployment.
