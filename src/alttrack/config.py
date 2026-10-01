"""Configuration of alttrack: paths, refresh and archive policies.

The configuration lives in ``$ALTTRACK_CONFIG`` (default
``~/.config/alttrack/config.toml``) and is a plain TOML file.  Every value can
be overridden through the ``ALTTRACK_*`` environment variables, and the most
frequently used knobs (database path, intervals) can be overridden per
invocation on the command line.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from pathlib import Path

API_BASE_URL = "https://rdb.altlinux.org/api"

# Minutes by default; kept as minutes everywhere in this module.
DEFAULTS: dict[str, object] = {
    "api_base_url": API_BASE_URL,
    "refresh_interval": 30,        # minutes between repository refreshes
    "task_history_days": 7,        # consider only fresh build tasks
    "backfill_limit": 50,          # build history imported on `watch add`
    "archive_after_days": 90,      # journal rows older than this are archived
    "max_live_rows": 5000,         # ... and the live journal is trimmed to this
    "auto_archive": True,          # archive automatically on refresh
    "http_timeout": 30.0,          # seconds
    "http_concurrency": 8,         # parallel requests to the ALTRepo API
    "host": "127.0.0.1",
    "port": 8300,
}


def default_config_path() -> Path:
    env = os.environ.get("ALTTRACK_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "alttrack" / "config.toml"


def default_data_dir() -> Path:
    env = os.environ.get("ALTTRACK_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "alttrack"


@dataclasses.dataclass
class Config:
    """Resolved configuration."""

    api_base_url: str = API_BASE_URL
    db_path: Path = dataclasses.field(default_factory=lambda: default_data_dir() / "alttrack.db")
    config_path: Path = dataclasses.field(default_factory=default_config_path)
    refresh_interval: int = 30
    task_history_days: int = 7
    backfill_limit: int = 50
    archive_after_days: int = 90
    max_live_rows: int = 5000
    auto_archive: bool = True
    http_timeout: float = 30.0
    http_concurrency: int = 8
    host: str = "127.0.0.1"
    port: int = 8300

    def to_dict(self) -> dict[str, object]:
        data = dataclasses.asdict(self)
        data["db_path"] = str(self.db_path)
        data["config_path"] = str(self.config_path)
        return data


ENV_OVERRIDES: dict[str, tuple[str, type]] = {
    "ALTTRACK_API_BASE_URL": ("api_base_url", str),
    "ALTTRACK_DB": ("db_path", Path),
    "ALTTRACK_REFRESH_INTERVAL": ("refresh_interval", int),
    "ALTTRACK_TASK_HISTORY_DAYS": ("task_history_days", int),
    "ALTTRACK_BACKFILL_LIMIT": ("backfill_limit", int),
    "ALTTRACK_ARCHIVE_AFTER_DAYS": ("archive_after_days", int),
    "ALTTRACK_MAX_LIVE_ROWS": ("max_live_rows", int),
    "ALTTRACK_AUTO_ARCHIVE": ("auto_archive", bool),
    "ALTTRACK_HTTP_TIMEOUT": ("http_timeout", float),
    "ALTTRACK_HTTP_CONCURRENCY": ("http_concurrency", int),
    "ALTTRACK_HOST": ("host", str),
    "ALTTRACK_PORT": ("port", int),
}


def _coerce(raw: object, target: type) -> object:
    if target is Path:
        return Path(str(raw)).expanduser()
    if target is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    return str(raw)


def load_config(config_path: Path | None = None, db_path: Path | None = None) -> Config:
    """Load configuration: defaults -> TOML file -> environment -> arguments."""
    path = config_path or default_config_path()
    cfg = Config(config_path=path)

    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        for key, value in data.items():
            if hasattr(cfg, key) and key not in {"config_path"}:
                setattr(cfg, key, _coerce(value, type(getattr(cfg, key))))

    for env_name, (attr, target) in ENV_OVERRIDES.items():
        if env_name in os.environ:
            setattr(cfg, attr, _coerce(os.environ[env_name], target))

    if db_path is not None:
        cfg.db_path = Path(db_path).expanduser()
    return cfg


def write_toml(cfg: Config) -> None:
    """Write the configuration back to its TOML file (used by the web UI)."""
    path = cfg.config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# alttrack configuration", ""]
    for field in dataclasses.fields(cfg):
        if field.name in {"config_path"}:
            continue
        value = getattr(cfg, field.name)
        if isinstance(value, Path):
            rendered = f'"{value}"'
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, str):
            rendered = f'"{value}"'
        else:
            rendered = str(value)
        lines.append(f"{field.name} = {rendered}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
