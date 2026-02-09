import os
import yaml

_config = None

def get_config(path: str | None = None) -> dict:
    """Load and cache YAML config."""
    global _config
    if _config is not None:
        return _config

    cfg_path = path or os.environ.get("HUMBLEVOICE_CONFIG")
    if not cfg_path:
        cfg_path = os.path.join(os.path.dirname(__file__), "config.yaml")

    with open(cfg_path, "r", encoding="utf-8") as f:
        _config = yaml.safe_load(f) or {}

    return _config
