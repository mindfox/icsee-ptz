from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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


def load_config(path: str | Path) -> ProxyConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    camera_rows = raw.get("cameras")
    if not isinstance(camera_rows, list) or not camera_rows:
        raise ValueError("configuration must contain a non-empty cameras list")

    cameras: list[CameraConfig] = []
    ids: set[str] = set()
    ports: set[tuple[str, int]] = set()

    for index, row in enumerate(camera_rows):
        if not isinstance(row, dict):
            raise ValueError(f"cameras[{index}] must be a mapping")
        camera_id = _required(row.get("id"), f"cameras[{index}].id")
        driver = _required(row.get("driver"), f"cameras[{index}].driver")
        host = _required(row.get("host"), f"cameras[{index}].host")
        listen = row.get("listen") or {}
        listen_host = str(listen.get("host", "0.0.0.0"))
        listen_port = int(listen.get("port", 8999))

        if camera_id in ids:
            raise ValueError(f"duplicate camera id: {camera_id}")
        endpoint = (listen_host, listen_port)
        if endpoint in ports:
            raise ValueError(f"duplicate listener: {listen_host}:{listen_port}")
        if not 1 <= listen_port <= 65535:
            raise ValueError(f"invalid listener port for {camera_id}: {listen_port}")

        ids.add(camera_id)
        ports.add(endpoint)
        cameras.append(
            CameraConfig(
                camera_id=camera_id,
                name=str(row.get("name") or camera_id),
                driver=driver,
                host=host,
                listen_host=listen_host,
                listen_port=listen_port,
                username=row.get("username"),
                password=row.get("password"),
                options=dict(row.get("options") or {}),
            )
        )

    web = raw.get("web") or {}
    return ProxyConfig(
        cameras=tuple(cameras),
        web_host=str(web.get("host", "0.0.0.0")),
        web_port=int(web.get("port", 8080)),
    )
