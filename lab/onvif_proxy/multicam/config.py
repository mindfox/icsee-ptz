from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml

SUPPORTED_DRIVERS = {"icsee_onvif", "tapo_c200"}

@dataclass(frozen=True)
class CameraConfig:
    camera_id: str
    name: str
    driver: str
    host: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 8999
    username: str | None = None
    password: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ProxyConfig:
    cameras: tuple[CameraConfig, ...]
    web_host: str = "0.0.0.0"
    web_port: int = 8080

def _required(value: Any, path: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"missing required configuration: {path}")
    return text

def _port(value: Any, path: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid port for {path}: {value!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port for {path}: {port}")
    return port

def load_config(path: str | Path) -> ProxyConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("configuration root must be a mapping")
    camera_rows = raw.get("cameras")
    if not isinstance(camera_rows, list) or not camera_rows:
        raise ValueError("configuration must contain a non-empty cameras list")

    web = raw.get("web") or {}
    if not isinstance(web, dict):
        raise ValueError("web must be a mapping")
    web_host = str(web.get("host", "0.0.0.0")).strip() or "0.0.0.0"
    web_port = _port(web.get("port", 8080), "web.port")

    cameras=[]; ids=set(); listeners=set()
    for index,row in enumerate(camera_rows):
        if not isinstance(row,dict): raise ValueError(f"cameras[{index}] must be a mapping")
        camera_id=_required(row.get("id"),f"cameras[{index}].id")
        driver=_required(row.get("driver"),f"cameras[{index}].driver").lower()
        host=_required(row.get("host"),f"cameras[{index}].host")
        if driver not in SUPPORTED_DRIVERS: raise ValueError(f"unsupported driver for {camera_id}: {driver}")
        listen=row.get("listen") or {}
        if not isinstance(listen,dict): raise ValueError(f"cameras[{index}].listen must be a mapping")
        listen_host=str(listen.get("host","0.0.0.0")).strip() or "0.0.0.0"
        listen_port=_port(listen.get("port",8999),f"cameras[{index}].listen.port")
        if camera_id in ids: raise ValueError(f"duplicate camera id: {camera_id}")
        endpoint=(listen_host,listen_port)
        if endpoint in listeners: raise ValueError(f"duplicate listener: {listen_host}:{listen_port}")
        if listen_port==web_port and (listen_host==web_host or "0.0.0.0" in {listen_host,web_host}):
            raise ValueError(f"camera listener conflicts with web listener: {camera_id} port {listen_port}")
        options=row.get("options") or {}
        if not isinstance(options,dict): raise ValueError(f"cameras[{index}].options must be a mapping")
        ids.add(camera_id); listeners.add(endpoint)
        cameras.append(CameraConfig(camera_id=camera_id,name=str(row.get("name") or camera_id),driver=driver,host=host,listen_host=listen_host,listen_port=listen_port,username=row.get("username"),password=row.get("password"),options=dict(options)))
    return ProxyConfig(cameras=tuple(cameras),web_host=web_host,web_port=web_port)
