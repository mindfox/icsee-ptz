import os
import re
import xml.etree.ElementTree as ET
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


def parse_preset_name_aliases(value: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        token, separator, name = item.partition("=")
        token = token.strip()
        name = name.strip()
        if not separator or not token or not name:
            raise RuntimeError(
                "PRESET_NAME_ALIASES must use comma-separated token=name entries"
            )
        aliases[token] = name
    return aliases


def install_preset_name_compatibility(proxy_module: Any) -> None:
    aliases = parse_preset_name_aliases(
        os.environ.get("PRESET_NAME_ALIASES", "0=vertical")
    )
    original_transform_response = proxy_module.transform_response

    def transform_response(body: bytes, origin: str, action: str) -> bytes:
        transformed = original_transform_response(body, origin, action)
        if action != "GetPresets" or not transformed or not aliases:
            return transformed

        try:
            root = ET.fromstring(transformed)
        except ET.ParseError:
            return transformed

        changed = False
        for preset in proxy_module.find_all(root, "Preset"):
            token = (preset.attrib.get("token") or "").strip()
            alias = aliases.get(token)
            if not alias:
                continue

            name_nodes = [
                node
                for node in list(preset)
                if proxy_module.local_name(node.tag) == "Name"
            ]
            if name_nodes:
                if not (name_nodes[0].text or "").strip():
                    name_nodes[0].text = alias
                    changed = True
            else:
                name = ET.Element(f"{{{proxy_module.TT}}}Name")
                name.text = alias
                preset.insert(0, name)
                changed = True

        if not changed:
            return transformed

        proxy_module.log(
            "restored blank preset names using aliases="
            + ",".join(f"{token}={name}" for token, name in aliases.items())
        )
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    proxy_module.transform_response = transform_response


if __name__ == "__main__":
    load_environment()

    import proxy
    import relative_zoom

    install_preset_name_compatibility(proxy)
    proxy.log(
        "preset_name_aliases="
        + os.environ.get("PRESET_NAME_ALIASES", "0=vertical")
    )
    proxy.ThreadingHTTPServer(
        (proxy.LISTEN_HOST, proxy.LISTEN_PORT),
        proxy.ProxyHandler,
    ).serve_forever()
