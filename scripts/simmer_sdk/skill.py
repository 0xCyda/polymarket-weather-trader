from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _coerce(value: Any, typ: type[Any]) -> Any:
    if value is None:
        return None
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        return text in {"1", "true", "yes", "y", "on"}
    if typ is int:
        return int(float(value))
    if typ is float:
        return float(value)
    if typ is str:
        return str(value)
    return value


def get_config_path(script_file: str | os.PathLike[str]) -> Path:
    path = Path(script_file)
    return path.with_name("config.json")


def load_config(schema: dict[str, dict[str, Any]], script_file: str | os.PathLike[str], slug: str | None = None) -> dict[str, Any]:
    config_path = get_config_path(script_file)
    data: dict[str, Any] = {}
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
        except Exception:
            data = {}

    cfg: dict[str, Any] = {}
    for key, meta in schema.items():
        default = meta.get("default")
        env_name = meta.get("env")
        typ = meta.get("type", str)
        value = data.get(key, default)
        if env_name and env_name in os.environ:
            value = os.environ[env_name]
        cfg[key] = _coerce(value, typ)
    return cfg


def update_config(updates: dict[str, Any], script_file: str | os.PathLike[str]) -> dict[str, Any]:
    config_path = get_config_path(script_file)
    current: dict[str, Any] = {}
    if config_path.exists():
        try:
            current = json.loads(config_path.read_text())
        except Exception:
            current = {}
    current.update(updates)
    config_path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n")
    return current
