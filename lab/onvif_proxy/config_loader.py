import importlib.util
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

CONFIG_PATH = Path(os.environ.get("ONVIF_PROXY_CONFIG", "/app/config.yaml"))
MODULE_DIR = Path(__file__).resolve().parent
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:-|-)([^}]*))?\}")
_MODULE_LOAD_LOCK = threading.Lock()


@dataclass(frozen=True)
class CameraSpec:
    name: str
    listen_host: str
    listen_port: int
    environment: dict[str, str]
    preset_aliases: dict[str, str]


@dataclass
class CameraRuntime:
    spec: CameraSpec
    proxy: ModuleType
    server: Any
    thread: threading.Thread | None = None


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


def _expanded(value: Any) -> Any:
    if isinstance(value, str):
        return expand_environment(value)
    if isinstance(value, dict):
        return {key: _expanded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expanded(item) for item in value]
    return value


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must be a mapping")
    return value


def _number(value: Any, path: str, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{path} must be numeric") from exc
    if minimum is not None and result < minimum:
        raise RuntimeError(f"{path} must be >= {minimum:g}")
    return result


def _integer(value: Any, path: str, *, minimum: int = 1, maximum: int = 65535) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{path} must be an integer") from exc
    if result < minimum or result > maximum:
        raise RuntimeError(f"{path} must be between {minimum} and {maximum}")
    return result


def _required_text(mapping: dict[str, Any], key: str, path: str) -> str:
    value = str(mapping.get(key, "")).strip()
    if not value:
        raise RuntimeError(f"{path}.{key} is required")
    return value


def _bool_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse_config(path: Path = CONFIG_PATH) -> list[CameraSpec]:
    if not path.is_file():
        raise RuntimeError(f"configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        document = _expanded(yaml.safe_load(handle) or {})

    root = _mapping(document, "config")
    server = _mapping(root.get("server", {}), "server")
    defaults = _mapping(root.get("defaults", {}), "defaults")
    cameras = _mapping(root.get("cameras"), "cameras")
    if not cameras:
        raise RuntimeError("cameras must contain at least one camera")

    listen_host = str(server.get("listen_host", "0.0.0.0")).strip() or "0.0.0.0"
    seen_ports: dict[int, str] = {}
    specs: list[CameraSpec] = []

    for name, raw_camera in cameras.items():
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("camera names must be non-empty strings")
        camera_path = f"cameras.{name}"
        camera = _mapping(raw_camera, camera_path)
        upstream = _mapping(camera.get("upstream"), f"{camera_path}.upstream")
        ptz = _mapping(camera.get("ptz", {}), f"{camera_path}.ptz")
        relative = _mapping(ptz.get("relative_move", {}), f"{camera_path}.ptz.relative_move")
        zoom = _mapping(ptz.get("zoom", {}), f"{camera_path}.ptz.zoom")
        reset = _mapping(ptz.get("return_preset_zoom_reset", {}), f"{camera_path}.ptz.return_preset_zoom_reset")
        aliases_raw = _mapping(ptz.get("preset_aliases", {}), f"{camera_path}.ptz.preset_aliases")

        port = _integer(camera.get("listen_port"), f"{camera_path}.listen_port")
        if port in seen_ports:
            raise RuntimeError(
                f"duplicate listen_port {port} for cameras {seen_ports[port]!r} and {name!r}"
            )
        seen_ports[port] = name

        aliases = {str(token): str(alias).strip() for token, alias in aliases_raw.items()}
        if any(not token or not alias for token, alias in aliases.items()):
            raise RuntimeError(f"{camera_path}.ptz.preset_aliases contains an empty token or name")

        env = {
            "CAMERA_HOST": _required_text(upstream, "host", f"{camera_path}.upstream"),
            "CAMERA_ONVIF_PORT": str(_integer(upstream.get("onvif_port", 8899), f"{camera_path}.upstream.onvif_port")),
            "CAMERA_USERNAME": _required_text(upstream, "username", f"{camera_path}.upstream"),
            "CAMERA_PASSWORD": _required_text(upstream, "password", f"{camera_path}.upstream"),
            "PROXY_LISTEN_HOST": listen_host,
            "PROXY_LISTEN_PORT": str(port),
            "UPSTREAM_TIMEOUT": str(_number(defaults.get("upstream_timeout", 10), "defaults.upstream_timeout", minimum=0)),
            "UPSTREAM_RETRY_INITIAL_SECONDS": str(_number(defaults.get("retry_initial_seconds", 0.25), "defaults.retry_initial_seconds", minimum=0)),
            "UPSTREAM_RETRY_MAX_SECONDS": str(_number(defaults.get("retry_max_seconds", 2.0), "defaults.retry_max_seconds", minimum=0)),
            "CONTINUOUS_VELOCITY": str(_number(relative.get("velocity", 0.5), f"{camera_path}.ptz.relative_move.velocity")),
            "PULSE_MIN_SECONDS": str(_number(relative.get("pulse_min_seconds", 0.04), f"{camera_path}.ptz.relative_move.pulse_min_seconds", minimum=0)),
            "PULSE_SECONDS_PER_FOV": str(_number(relative.get("pulse_seconds_per_fov", 0.8), f"{camera_path}.ptz.relative_move.pulse_seconds_per_fov", minimum=0)),
            "PULSE_MAX_SECONDS": str(_number(relative.get("pulse_max_seconds", 1.0), f"{camera_path}.ptz.relative_move.pulse_max_seconds", minimum=0)),
            "STATUS_SETTLE_SECONDS": str(_number(relative.get("status_settle_seconds", 0.2), f"{camera_path}.ptz.relative_move.status_settle_seconds", minimum=0)),
            "RELATIVE_ZOOM_VELOCITY": str(_number(zoom.get("relative_velocity", 0.5), f"{camera_path}.ptz.zoom.relative_velocity")),
            "ABSOLUTE_ZOOM_FULL_TRAVEL_SECONDS": str(_number(zoom.get("absolute_full_travel_seconds", 8.0), f"{camera_path}.ptz.zoom.absolute_full_travel_seconds", minimum=0)),
            "RETURN_PRESET_ZOOM_RESET": _bool_string(reset.get("enabled", True)),
            "RETURN_PRESET_ZOOM_DELAY_SECONDS": str(_number(reset.get("delay_seconds", 0.25), f"{camera_path}.ptz.return_preset_zoom_reset.delay_seconds", minimum=0)),
            "RETURN_PRESET_ZOOM_VELOCITY": str(_number(reset.get("velocity", -0.5), f"{camera_path}.ptz.return_preset_zoom_reset.velocity")),
            "RETURN_PRESET_ZOOM_SECONDS": str(_number(reset.get("duration_seconds", 8.0), f"{camera_path}.ptz.return_preset_zoom_reset.duration_seconds", minimum=0)),
        }
        specs.append(CameraSpec(name=name, listen_host=listen_host, listen_port=port, environment=env, preset_aliases=aliases))

    return specs


@contextmanager
def _temporary_environment(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def install_preset_name_compatibility(proxy_module: ModuleType, aliases: dict[str, str]) -> None:
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
            names = [node for node in list(preset) if proxy_module.local_name(node.tag) == "Name"]
            if names:
                if not (names[0].text or "").strip():
                    names[0].text = alias
                    changed = True
            else:
                name = ET.Element(f"{{{proxy_module.TT}}}Name")
                name.text = alias
                preset.insert(0, name)
                changed = True
        if not changed:
            return transformed
        proxy_module.log("restored blank preset names using aliases=" + ",".join(f"{token}={alias}" for token, alias in aliases.items()))
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    proxy_module.transform_response = transform_response


def build_runtime(spec: CameraSpec) -> CameraRuntime:
    safe_name = re.sub(r"[^A-Za-z0-9_]", "_", spec.name)
    proxy_name = f"onvif_proxy_{safe_name}"
    zoom_name = f"onvif_relative_zoom_{safe_name}"

    with _MODULE_LOAD_LOCK, _temporary_environment(spec.environment):
        old_proxy_alias = sys.modules.get("proxy")
        try:
            proxy_module = _load_module(proxy_name, MODULE_DIR / "proxy.py")
            base_log = proxy_module.log
            proxy_module.log = lambda message, _base=base_log: _base(f"camera={spec.name} {message}")
            sys.modules["proxy"] = proxy_module
            _load_module(zoom_name, MODULE_DIR / "relative_zoom.py")
            install_preset_name_compatibility(proxy_module, spec.preset_aliases)
        finally:
            if old_proxy_alias is None:
                sys.modules.pop("proxy", None)
            else:
                sys.modules["proxy"] = old_proxy_alias

    server = proxy_module.ThreadingHTTPServer(
        (spec.listen_host, spec.listen_port), proxy_module.ProxyHandler
    )
    return CameraRuntime(spec=spec, proxy=proxy_module, server=server)


def run() -> None:
    specs = parse_config()
    runtimes: list[CameraRuntime] = []
    try:
        for spec in specs:
            runtimes.append(build_runtime(spec))
    except Exception:
        for runtime in runtimes:
            runtime.server.server_close()
        raise

    for runtime in runtimes:
        runtime.proxy.log(
            f"listener={runtime.spec.listen_host}:{runtime.spec.listen_port} upstream={runtime.proxy.UPSTREAM_ORIGIN}"
        )
        runtime.thread = threading.Thread(
            target=runtime.server.serve_forever,
            name=f"onvif-{runtime.spec.name}",
            daemon=False,
        )
        runtime.thread.start()

    try:
        for runtime in runtimes:
            runtime.thread.join()
    except KeyboardInterrupt:
        for runtime in runtimes:
            runtime.server.shutdown()
        for runtime in runtimes:
            runtime.thread.join()
    finally:
        for runtime in runtimes:
            runtime.server.server_close()


if __name__ == "__main__":
    run()
