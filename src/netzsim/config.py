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

    # Grid catalog (archive of importable grid models, served by /grids)
    grid_archive: Path = Path("European Archetpye Distribution Grid Models.zip")
    grid_filter: str = "Low Voltage Network Models/03_LV"
    # Pre-generated ding0 grids (eDisGo CSV export, with real lat/lon)
    ding0_dir: Path = Path("./data/ding0_grids")
    # Curated grid library manifest (MV/LV · size · rural/suburban/urban)
    grid_library: Path = Path("./data/grid_library.json")

    # Cached LPG household load library (served by /loadgen)
    lpg_library_dir: Path = Path("./data/lpg_library")

    # CORS (comma-separated origins; "*" allows any)
    cors_origins: str = "*"

    # Realtime clock (accelerated tick)
    step_interval_seconds: float = 1.0
    steps_per_day: int = 1440
    autostart: bool = True

    # Engine
    history_size: int = 1440
    warm_start: bool = True

    # API
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "info"


settings = Settings()
