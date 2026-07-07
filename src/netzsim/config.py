"""Runtime configuration, loaded from environment / .env file."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NETZSIM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Data
    data_dir: Path = Path("./data")

    # Grid catalog: a committed dataset produced by the separate `gridgen` project.
    # Pre-generated ding0 grids (eDisGo CSV export, with real lat/lon)
    ding0_dir: Path = Path("./data/ding0_grids")
    # Curated grid library manifest (MV/LV · size · rural/suburban/urban)
    grid_library: Path = Path("./data/grid_library.json")
    # User-drawn grids (gridformat JSON exported by the sibling gridedit tool);
    # scanned on every /grids listing, so new exports appear without a restart
    user_grids_dir: Path = Path("./data/user_grids")

    # Cached LPG household load library (served by /loadgen)
    scenarios_dir: str = "./data/scenarios"  # saved live-setup recipes (JSON)
    lpg_library_dir: Path = Path("./data/lpg_library")

    # Cached real-PV daily shapes (see scripts/fetch_real_pv.py). When present,
    # PV generation follows these real measured days and a day slider is offered.
    real_pv_file: Path = Path("./data/real_pv_days.json")

    # Cached hourly aWATTar prices (see scripts/fetch_awattar.py), aligned to the
    # PV days; drive the battery "price" strategy.
    awattar_file: Path = Path("./data/awattar_prices.json")

    # Session recordings (CSV export of every published step, see recorder.py).
    # record=True starts a recording on startup and after every grid apply /
    # scenario load (continuous operation); default is opt-in via the API/UI.
    recordings_dir: Path = Path("./data/recordings")
    record: bool = False

    # CORS (comma-separated origins; "*" allows any)
    cors_origins: str = "*"

    # Realtime clock (accelerated tick)
    step_interval_seconds: float = 1.0
    steps_per_day: int = 1440
    autostart: bool = True

    # Engine
    history_size: int = 1440
    warm_start: bool = True

    # Observability: when False, the live /state + /ws stream carries ONLY the
    # observed measurement projection (readings at placed meters) — the full
    # ground-truth power flow (per-bus/line/trafo results + system summary) never
    # leaves the server. Default True so the UI's "reveal ground truth" toggle and
    # the InfluxDB collector keep working out of the box; set False to enforce
    # strict observability on the wire.
    expose_ground_truth: bool = True

    # API — bind loopback by default (there are write/control endpoints and no
    # auth); Docker / LAN demos opt in explicitly via NETZSIM_HOST=0.0.0.0.
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"


settings = Settings()
