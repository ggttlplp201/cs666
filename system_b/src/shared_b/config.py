"""Config loading (Shared §12: knobs live in versioned YAML, not code).

`load_config("b")` deep-merges config/shared.yaml <- config/system_b.yaml and
returns a dot-accessible mapping. Editing YAML + reload changes behavior with
no redeploy. Secrets come only from the environment / .env (never YAML).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


class Cfg(dict):
    """Read-only-ish dict with attribute access and safe .get chains."""

    def __getattr__(self, key: str) -> Any:
        try:
            v = self[key]
        except KeyError as e:
            raise AttributeError(f"missing config key: {key}") from e
        return Cfg(v) if isinstance(v, dict) else v

    def at(self, path: str, default: Any = None) -> Any:
        """cfg.at('risk_controls.daily_loss_limit_pct', -0.05)"""
        node: Any = self
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(system: str | None = "b", config_dir: Path | None = None) -> Cfg:
    cdir = config_dir or CONFIG_DIR
    merged = load_yaml(cdir / "shared.yaml")
    if system:
        merged = _deep_merge(merged, load_yaml(cdir / f"system_{system}.yaml"))
    return Cfg(merged)


def env_secret(name: str, default: str | None = None) -> str | None:
    """Secrets only ever come from the environment (.env loaded by the shell),
    never from YAML — HANDOFF 'Credentials safety'."""
    v = os.environ.get(name, default)
    return v if v else None
