"""Configuration loading.

Values come from ``config.toml`` next to the project root, falling back to the
defaults defined here.  Unknown keys are ignored so a config file written for a
newer version still loads.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, dict[str, Any]] = {
    "server": {
        "host": "0.0.0.0",
        "port": 8443,
        "cert_file": "data/certs/server.crt",
        "key_file": "data/certs/server.key",
        "cert_hostnames": [],
    },
    "data_node": {
        "host": "127.0.0.1",
        "port": 980,
        "variables": ["*"],
        "reconnect_min_delay": 1.0,
        "reconnect_max_delay": 30.0,
        "backfill_seconds": 3600,
        "allow_history_requests": True,
    },
    "store": {
        "max_bytes": 4_000_000_000,
        "max_age_seconds": 0,
    },
    "plots": {
        "max_points_per_series": 4000,
        "live_refresh_ms": 1000,
    },
    "security": {
        "allow_registration": True,
        "allow_user_functions": True,
        "session_lifetime_hours": 12,
    },
}


class Config:
    """Merged view of the defaults and the user's ``config.toml``."""

    def __init__(self, data: dict[str, Any] | None = None, root: Path | None = None):
        self.root = Path(root or ROOT)
        merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in DEFAULTS.items()}
        for section, values in (data or {}).items():
            if section in merged and isinstance(values, dict):
                merged[section].update(values)
            else:
                merged[section] = values
        self._data = merged

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None, root: Path | None = None) -> "Config":
        root = Path(root or ROOT)
        candidate = Path(path) if path else root / "config.toml"
        data: dict[str, Any] = {}
        if candidate.is_file():
            with open(candidate, "rb") as fh:
                data = tomllib.load(fh)
        return cls(data, root=root)

    def section(self, name: str) -> dict[str, Any]:
        return self._data.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self._data.get(section, {}).get(key, DEFAULTS.get(section, {}).get(key, default))

    def path(self, section: str, key: str) -> Path:
        """Resolve a configured path relative to the project root."""
        value = Path(str(self.get(section, key)))
        return value if value.is_absolute() else self.root / value

    # -- Well-known directories -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def profiles_dir(self) -> Path:
        return self.root / "Profiles"

    @property
    def functions_dir(self) -> Path:
        return self.root / "Functions"
