from pathlib import Path
import yaml
from types import SimpleNamespace

def _deep_merge(base: dict, override: dict) -> dict:
    merged = base.copy()
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged

def _to_namespace(d: dict) -> SimpleNamespace:
    ns = SimpleNamespace()
    for key, value in d.items():
        setattr(ns, key, _to_namespace(value) if isinstance(value, dict) else value)
    return ns

def _load() -> SimpleNamespace:
    base_path = Path(__file__).parent / "settings" / "defaults.yaml"
    user_path = Path(__file__).parent / "settings" / "user.yaml"

    with open(base_path) as f:
        data = yaml.safe_load(f)

    if user_path.exists():
        with open(user_path) as f:
            user_data = yaml.safe_load(f) or {}
        data = _deep_merge(data, user_data)

    return _to_namespace(data)

settings = _load()