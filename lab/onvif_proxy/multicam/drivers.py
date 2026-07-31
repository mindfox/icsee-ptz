from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from .config import CameraConfig


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
            "capabilities": self.capabilities.__dict__,
        }


class IcseeOnvifDriver(CameraDriver):
    """Marker driver for the existing proxy implementation.

    The currently tested ONVIF translation remains in proxy.py. The multicamera
    launcher starts one legacy proxy process per camera and uses this class for
    capability discovery in the shared UI.
    """

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(pan_tilt=True, zoom=True, presets=True)

    def move(self, pan: float, tilt: float, duration: float) -> None:
        raise RuntimeError("iCSee movement is handled by the per-camera legacy ONVIF listener")

    def stop(self) -> None:
        raise RuntimeError("iCSee stop is handled by the per-camera legacy ONVIF listener")


class TapoC200Driver(CameraDriver):
    """Local Tapo C200 PTZ driver using pytapo.

    Tapo firmware expects commands in strict sequence. All calls are serialized
    per camera with a re-entrant lock; no parallel requests are issued.
    """

    def __init__(self, config: CameraConfig):
        super().__init__(config)
        if not config.username or not config.password:
            raise ValueError(f"{config.camera_id}: Tapo username and password are required")
        try:
            from pytapo import Tapo
        except ImportError as exc:
            raise RuntimeError("pytapo is required for the tapo_c200 driver") from exc

        self._lock = threading.RLock()
        self._camera = Tapo(config.host, config.username, config.password)
        self._step = int(config.options.get("step", 10))

    @property
    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(pan_tilt=True, zoom=False, presets=True, audio=True)

    def move(self, pan: float, tilt: float, duration: float) -> None:
        del duration  # Tapo exposes discrete motor steps rather than timed velocity.
        horizontal = round(max(-1.0, min(1.0, pan)) * self._step)
        vertical = round(max(-1.0, min(1.0, tilt)) * self._step)
        if horizontal == 0 and vertical == 0:
            return
        with self._lock:
            self._camera.moveMotor(horizontal, vertical)

    def stop(self) -> None:
        # moveMotor is a bounded discrete move; there is no continuous motion to stop.
        return

    def status(self) -> dict[str, Any]:
        with self._lock:
            info = self._camera.getBasicInfo()
        status = super().status()
        status["device"] = info
        return status
