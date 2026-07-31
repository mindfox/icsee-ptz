from __future__ import annotations

from collections.abc import Callable

from .config import CameraConfig
from .drivers import CameraDriver, IcseeOnvifDriver, TapoC200Driver

DriverFactory = Callable[[CameraConfig], CameraDriver]


class DriverRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, DriverFactory] = {}

    def register(self, name: str, factory: DriverFactory) -> None:
        key = name.strip().lower()
        if not key:
            raise ValueError("driver name cannot be empty")
        if key in self._factories:
            raise ValueError(f"driver already registered: {key}")
        self._factories[key] = factory

    def create(self, config: CameraConfig) -> CameraDriver:
        key = config.driver.strip().lower()
        try:
            factory = self._factories[key]
        except KeyError as exc:
            available = ", ".join(sorted(self._factories)) or "none"
            raise ValueError(f"unknown driver {config.driver!r}; available: {available}") from exc
        return factory(config)

    @classmethod
    def defaults(cls) -> "DriverRegistry":
        registry = cls()
        registry.register("icsee_onvif", IcseeOnvifDriver)
        registry.register("tapo_c200", TapoC200Driver)
        return registry
