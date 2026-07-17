"""Config + secrets loading.

All tunables come from config/*.yaml (shared.yaml overlaid by the system
file) — code never hardcodes weights/thresholds/caps. Secrets come from .env
only; a PLACEHOLDER value means "not yet supplied" and any component needing
that secret must degrade gracefully instead of calling out with a bad key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PLACEHOLDER = "PLACEHOLDER"


def load_env(repo_root: Path) -> None:
    """Populate os.environ from .env without overriding existing values."""
    env_file = repo_root / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("#", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value


def secret(name: str) -> str | None:
    """Return a usable secret, or None if missing/placeholder."""
    value = os.environ.get(name, "").strip()
    if not value or value.upper() == PLACEHOLDER:
        return None
    return value


def _deep_merge(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for k, v in overlay.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


class Config:
    """Dot-path access over shared.yaml (+ optional system overlay)."""

    def __init__(self, data: dict[str, Any], repo_root: Path):
        self.data = data
        self.repo_root = repo_root

    @classmethod
    def load(cls, repo_root: Path, system: str | None = None) -> "Config":
        load_env(repo_root)
        shared = yaml.safe_load((repo_root / "config" / "shared.yaml").read_text())
        if system:
            overlay = yaml.safe_load(
                (repo_root / "config" / f"{system}.yaml").read_text()
            )
            shared = _deep_merge(shared, {system: overlay})
        return cls(shared, repo_root)

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, path: str) -> Any:
        value = self.get(path, default=None)
        if value is None:
            raise KeyError(f"missing required config key: {path}")
        return value
