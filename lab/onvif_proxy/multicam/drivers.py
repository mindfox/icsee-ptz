from __future__ import annotations

import threading
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

import requests

from .config import CameraConfig

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"
FOV_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"


@dataclass(frozen=True)
class CameraCapabilities:
    pan_tilt: bool
    zoom: bool = False
    presets: bool = False
    audio: bool = False


class CameraDriver(ABC):
    def __init__(self, config: CameraConfig):
        self.config = config

    @property
    @abstractmethod
    def capabilities(self) -> CameraCapabilities:
        raise NotImplementedError

    @abstractmethod
    def move(self, pan: float, tilt: float, duration: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    def status(self) -> dict[str, Any]:
        return {
            "id": self.config.camera_id,
            "name": self.config.name,
            "driver": self.config.driver,
            "host": self.config.host,
            "listen": f"{self.config.listen_host}:{self.config.listen_port}",
            "available": True,
            "connection_state": "available",
            "capabilities": asdict(self.capabilities),
        }


def _soap(operation: str, *, pan: float = 0, tilt: float = 0) -> bytes:
    envelope = ET.Element(f"{{{SOAP12}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP12}}}Body")
    op = ET.SubElement(body, f"{{{TPTZ}}}{operation}")
    ET.SubElement(op, f"{{{TPTZ}}}ProfileToken").text = "000"
    if operation == "RelativeMove":
        translation = ET.SubElement(op, f"{{{TPTZ}}}Translation")
        node = ET.SubElement(translation, f"{{{TT}}}PanTilt")
        node.set("x", str(pan))
        node.set("y", str(tilt))
        node.set("space", FOV_SPACE)
    elif operation == "Stop":
        ET.SubElement(op, f"{{{TPTZ}}}PanTilt").text = "true"
        ET.SubElement(op, f"{{{TPTZ}}}Zoom").text = "false"
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


class IcseeOnvifDriver(CameraDriver):
    """Manual-control adapter for the existing per-camera proxy listener.

    The dashboard must use the same RelativeMove translation path that was
    already validated with Frigate. It must not maintain a second direct PTZ
    implementation for this camera family.
    """

    def __init__(self, config: CameraConfig):
        super().__init__(config)
        self._lock = threading.RLock()
        self._host = "127.0.0.1"
        self._port = config.listen_port
        self._path = str(config.options.get("ptz_path", "/onvif/ptz_service"))
        self._timeout = float(config.options.get("timeout", 10))
        self._ui_step = float(config.options.get("ui_relative_step", 0.1))
        if not 0 < self._ui_step <= 1:
            raise ValueError(
                f"{config.camera_id}: ui_relative_step must be greater than 0 and at most 1"
            )

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(pan_tilt=True, zoom=True, presets=True)

    def _post(self, operation: str, payload: bytes) -> None:
        response = requests.post(
            f"http://{self._host}:{self._port}{self._path}",
            data=payload,
            headers={
                "Content-Type": (
                    "application/soap+xml; charset=utf-8; "
                    f'action="{TPTZ}/{operation}"'
                )
            },
            timeout=self._timeout,
        )
        response.raise_for_status()

    def move(self, pan: float, tilt: float, duration: float) -> None:
        del duration
        pan = max(-1.0, min(1.0, pan)) * self._ui_step
        tilt = max(-1.0, min(1.0, tilt)) * self._ui_step
        if abs(pan) < 1e-9 and abs(tilt) < 1e-9:
            return
        with self._lock:
            self._post("RelativeMove", _soap("RelativeMove", pan=pan, tilt=tilt))

    def stop(self) -> None:
        with self._lock:
            self._post("Stop", _soap("Stop"))


class TapoC200Driver(CameraDriver):
    """Tapo motor driver with lazy private-API initialization.

    Constructing the multi-camera runtime must never contact a camera. Some Tapo
    firmware revisions reject pytapo's private authentication while RTSP/ONVIF
    remain usable. The failure is therefore contained to commands for this
    camera instead of terminating every listener and the shared web UI.
    """

    def __init__(self, config: CameraConfig):
        super().__init__(config)
        if not config.username or not config.password:
            raise ValueError(f"{config.camera_id}: Tapo username and password are required")

        self._lock = threading.RLock()
        self._camera: Any | None = None
        self._connection_error: str | None = None
        self._connection_attempted = False
        self._step = int(config.options.get("step", 10))
        if self._step < 1:
            raise ValueError(f"{config.camera_id}: step must be positive")

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(pan_tilt=True, zoom=False, presets=False, audio=False)

    def _connect(self) -> Any:
        if self._camera is not None:
            return self._camera

        self._connection_attempted = True
        try:
            from pytapo import Tapo
        except ImportError as exc:
            self._connection_error = "pytapo is not installed"
            raise RuntimeError(self._connection_error) from exc

        try:
            self._camera = Tapo(
                self.config.host,
                self.config.username,
                self.config.password,
            )
        except Exception as exc:
            self._connection_error = f"{type(exc).__name__}: {exc}"
            raise RuntimeError(
                f"{self.config.camera_id}: Tapo private API unavailable: "
                f"{self._connection_error}"
            ) from exc

        self._connection_error = None
        return self._camera

    def move(self, pan: float, tilt: float, duration: float) -> None:
        del duration
        horizontal = round(max(-1.0, min(1.0, pan)) * self._step)
        vertical = round(max(-1.0, min(1.0, tilt)) * self._step)
        if bool(self.config.options.get("invert_pan", False)):
            horizontal = -horizontal
        if bool(self.config.options.get("invert_tilt", False)):
            vertical = -vertical
        if horizontal == 0 and vertical == 0:
            return

        with self._lock:
            self._connect().moveMotor(horizontal, vertical)

    def stop(self) -> None:
        return

    def status(self) -> dict[str, Any]:
        status = super().status()
        if not self._connection_attempted:
            status["available"] = None
            status["connection_state"] = "untested"
        elif self._connection_error is not None:
            status["available"] = False
            status["connection_state"] = "unavailable"
            status["error"] = self._connection_error
        else:
            status["available"] = True
            status["connection_state"] = "available"

        if bool(self.config.options.get("query_device_info", False)):
            try:
                with self._lock:
                    status["device"] = self._connect().getBasicInfo()
                status["available"] = True
                status["connection_state"] = "available"
                status.pop("error", None)
            except Exception as exc:
                status["available"] = False
                status["connection_state"] = "unavailable"
                status["error"] = f"{type(exc).__name__}: {exc}"
        return status
