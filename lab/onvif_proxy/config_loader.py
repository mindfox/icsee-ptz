import os
import re
import runpy
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(os.environ.get("ONVIF_PROXY_CONFIG", "/app/config.yaml"))
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\}")


def expand_environment(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, operator, default = match.groups()
        current = os.environ.get(name)
        if operator == ":-":
            return current if current not in (None, "") else (default or "")
        if operator == "-":
            return current if current is not None else (default or "")
        if current is None:
            raise RuntimeError(f"required environment variable {name!r} is not set")
        return current

    return _ENV_PATTERN.sub(replace, value)


def scalar_to_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def load_environment() -> None:
    if not CONFIG_PATH.is_file():
        raise RuntimeError(f"configuration file not found: {CONFIG_PATH}")

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    settings = document.get("environment")
    if not isinstance(settings, dict):
        raise RuntimeError("config.yaml must contain an 'environment' mapping")

    for name, raw_value in settings.items():
        if not isinstance(name, str) or not name:
            raise RuntimeError("all environment setting names must be non-empty strings")
        value = scalar_to_string(raw_value)
        os.environ[name] = expand_environment(value)


if __name__ == "__main__":
    load_environment()
    runpy.run_path("/app/relative_zoom.py", run_name="__main__")
