from __future__ import annotations

import threading
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any

import requests

from .config import CameraConfig
from .tapo_onvif import TapoOnvifClient

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"
FOV_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"


@dataclass(frozen=True)
class CameraCapabilities:
    pan_tilt: bool
    zoom: bool = False
    presets: bool = False
    home: bool = False
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
    def __init__(self, config: CameraConfig):
        super().__init__(config)
        self._lock = threading.RLock()
        self._host = "127.0.0.1"
        self._port = config.listen_port
        self._path = str(config.options.get("ptz_path", "/onvif/ptz_service"))
        self._timeout = float(config.options.get("timeout", 10))
        self._ui_step = float(config.options.get("ui_relative_step", 0.1))
        if not 0 < self._ui_step <= 1:
            raise ValueError(f"{config.camera_id}: ui_relative_step must be greater than 0 and at most 1")

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(pan_tilt=True, zoom=True, presets=True)

    def _post(self, operation: str, payload: bytes) -> None:
        response = requests.post(
            f"http://{self._host}:{self._port}{self._path}",
            data=payload,
            headers={"Content-Type": f'application/soap+xml; charset=utf-8; action="{TPTZ}/{operation}"'},
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
    """Tapo C200 native ONVIF driver used by the listener and dashboard."""

    def __init__(self, config: CameraConfig):
        super().__init__(config)
        if not config.username or not config.password:
            raise ValueError(f"{config.camera_id}: Tapo username and password are required")
        self._lock = threading.RLock()
        self._client = TapoOnvifClient(config)
        self._connection_error: str | None = None
        self._connection_attempted = False
        self._velocity = float(config.options.get("continuous_velocity", 0.5))
        self._pulse_seconds = float(config.options.get("pulse_seconds", 0.10))
        if not 0 < self._velocity <= 1:
            raise ValueError(f"{config.camera_id}: continuous_velocity must be greater than 0 and at most 1")
        if not 0.02 <= self._pulse_seconds <= 2:
            raise ValueError(f"{config.camera_id}: pulse_seconds must be between 0.02 and 2")

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(pan_tilt=True, zoom=False, presets=True, home=True, audio=False)

    def _success(self):
        self._connection_attempted = True
        self._connection_error = None

    def _failure(self, exc: Exception):
        self._connection_attempted = True
        self._connection_error = f"{type(exc).__name__}: {exc}"

    def _call(self, operation: str, function, *args):
        with self._lock:
            try:
                result = function(*args)
                self._success()
                return result
            except Exception as exc:
                self._failure(exc)
                raise RuntimeError(f"{self.config.camera_id}: native ONVIF {operation} failed: {self._connection_error}") from exc

    def move(self, pan: float, tilt: float, duration: float) -> None:
        pan = max(-1.0, min(1.0, pan))
        tilt = max(-1.0, min(1.0, tilt))
        if bool(self.config.options.get("invert_pan", False)):
            pan = -pan
        if bool(self.config.options.get("invert_tilt", False)):
            tilt = -tilt
        if abs(pan) < 1e-9 and abs(tilt) < 1e-9:
            return
        pulse = duration if duration > 0 else self._pulse_seconds
        self._call("movement", self._client.continuous_move, pan * self._velocity, tilt * self._velocity, max(0.02, min(2.0, pulse)))

    def stop(self) -> None:
        self._call("stop", self._client.stop)

    def ptz_status(self):
        return self._call("status", self._client.get_status)

    def presets(self):
        return self._call("GetPresets", self._client.get_presets)

    def set_preset(self, token: str, name: str):
        return self._call("SetPreset", self._client.set_preset, token, name)

    def goto_preset(self, token: str):
        return self._call("GotoPreset", self._client.goto_preset, token)

    def remove_preset(self, token: str):
        return self._call("RemovePreset", self._client.remove_preset, token)

    def goto_home(self):
        return self._call("GotoHomePosition", self._client.goto_home)

    def set_home(self):
        return self._call("SetHomePosition", self._client.set_home)

    def diagnostics(self):
        return self._call("diagnostics", self._client.diagnostics)

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
                status["ptz_status"] = self.ptz_status()
                status["available"] = True
                status["connection_state"] = "available"
                status.pop("error", None)
            except Exception:
                status["available"] = False
                status["connection_state"] = "unavailable"
                status["error"] = self._connection_error
        return status
