from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_DRIVERS = {"icsee_onvif", "tapo_c200"}
SUPPORTED_FEED_SOURCES = {"disabled", "restream", "direct_rtsp", "dvrip"}


@dataclass(frozen=True)
class FeedConfig:
    source: str = "disabled"
    url: str | None = None
    transport: str = "tcp"
    fps: float = 1.0
    width: int = 960
    start_timeout: float = 15.0
    stop_timeout: float = 5.0
    retry_seconds: float = 5.0
    ffmpeg_binary: str = "ffmpeg"
    dvrip_port: int = 34567
    dvrip_module_path: str = "/opt/icsee_ptz/asyncio_dvrip.py"


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
    feed: FeedConfig = field(default_factory=FeedConfig)


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


def _float_range(value: Any, path: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid number for {path}: {value!r}") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{path} must be between {minimum:g} and {maximum:g}")
    return parsed


def _int_range(value: Any, path: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer for {path}: {value!r}") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{path} must be between {minimum} and {maximum}")
    return parsed


def _feed_config(row: dict[str, Any], index: int) -> FeedConfig:
    raw = row.get("feed") or {}
    path = f"cameras[{index}].feed"
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping")

    source = str(raw.get("source", "disabled")).strip().lower() or "disabled"
    if source not in SUPPORTED_FEED_SOURCES:
        raise ValueError(f"unsupported feed source for {path}: {source}")

    url_value = raw.get("url")
    url = str(url_value).strip() if url_value is not None else None
    if source in {"restream", "direct_rtsp"} and not url:
        raise ValueError(f"{path}.url is required for source={source}")
    if source == "disabled" and url:
        raise ValueError(f"{path}.url is not valid when source=disabled")

    transport = str(raw.get("transport", "tcp")).strip().lower() or "tcp"
    if transport not in {"tcp", "udp"}:
        raise ValueError(f"{path}.transport must be tcp or udp")

    return FeedConfig(
        source=source,
        url=url,
        transport=transport,
        fps=_float_range(raw.get("fps", 1), f"{path}.fps", 0.1, 10),
        width=_int_range(raw.get("width", 960), f"{path}.width", 160, 3840),
        start_timeout=_float_range(
            raw.get("start_timeout", 15), f"{path}.start_timeout", 1, 120
        ),
        stop_timeout=_float_range(
            raw.get("stop_timeout", 5), f"{path}.stop_timeout", 1, 30
        ),
        retry_seconds=_float_range(
            raw.get("retry_seconds", 5), f"{path}.retry_seconds", 1, 300
        ),
        ffmpeg_binary=str(raw.get("ffmpeg_binary", "ffmpeg")).strip() or "ffmpeg",
        dvrip_port=_port(raw.get("port", 34567), f"{path}.port"),
        dvrip_module_path=str(
            raw.get("module_path", "/opt/icsee_ptz/asyncio_dvrip.py")
        ).strip()
        or "/opt/icsee_ptz/asyncio_dvrip.py",
    )


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

    cameras = []
    ids = set()
    listeners = set()
    for index, row in enumerate(camera_rows):
        if not isinstance(row, dict):
            raise ValueError(f"cameras[{index}] must be a mapping")
        camera_id = _required(row.get("id"), f"cameras[{index}].id")
        driver = _required(row.get("driver"), f"cameras[{index}].driver").lower()
        host = _required(row.get("host"), f"cameras[{index}].host")
        if driver not in SUPPORTED_DRIVERS:
            raise ValueError(f"unsupported driver for {camera_id}: {driver}")
        listen = row.get("listen") or {}
        if not isinstance(listen, dict):
            raise ValueError(f"cameras[{index}].listen must be a mapping")
        listen_host = str(listen.get("host", "0.0.0.0")).strip() or "0.0.0.0"
        listen_port = _port(listen.get("port", 8999), f"cameras[{index}].listen.port")
        if camera_id in ids:
            raise ValueError(f"duplicate camera id: {camera_id}")
        endpoint = (listen_host, listen_port)
        if endpoint in listeners:
            raise ValueError(f"duplicate listener: {listen_host}:{listen_port}")
        if listen_port == web_port and (
            listen_host == web_host or "0.0.0.0" in {listen_host, web_host}
        ):
            raise ValueError(
                f"camera listener conflicts with web listener: {camera_id} port {listen_port}"
            )
        options = row.get("options") or {}
        if not isinstance(options, dict):
            raise ValueError(f"cameras[{index}].options must be a mapping")
        ids.add(camera_id)
        listeners.add(endpoint)
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
                options=dict(options),
                feed=_feed_config(row, index),
            )
        )
    return ProxyConfig(cameras=tuple(cameras), web_host=web_host, web_port=web_port)
