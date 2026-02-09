import os
import yaml

_config = None


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base without mutating inputs."""
    result = dict(base or {})
    for key, val in (override or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result

def get_config(path: str | None = None) -> dict:
    """Load and cache YAML config."""
    global _config
    if _config is not None:
        return _config

    cfg_path = path or os.environ.get("HUMBLEVOICE_CONFIG")
    if not cfg_path:
        cfg_path = os.path.join(os.path.dirname(__file__), "config.yaml")

    with open(cfg_path, "r", encoding="utf-8") as f:
        base_cfg = yaml.safe_load(f) or {}

    private_path = os.environ.get("HUMBLEVOICE_PRIVATE_CONFIG") or os.path.join(os.path.dirname(cfg_path), "config.private.yaml")
    if os.path.exists(private_path):
        with open(private_path, "r", encoding="utf-8") as f:
            private_cfg = yaml.safe_load(f) or {}
        _config = _deep_merge(base_cfg, private_cfg)
    else:
        _config = base_cfg

    return _config
