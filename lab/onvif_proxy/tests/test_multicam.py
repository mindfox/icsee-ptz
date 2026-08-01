import sys
import types
from pathlib import Path

import pytest

from multicam.config import CameraConfig, ProxyConfig, load_config
from multicam.drivers import TapoC200Driver
from multicam.registry import DriverRegistry
from multicam.runtime import CameraRuntime


def test_loads_multiple_cameras(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
cameras:
  - id: one
    driver: icsee_onvif
    host: 10.0.0.1
    listen: {port: 8999}
  - id: two
    driver: icsee_onvif
    host: 10.0.0.2
    listen: {port: 9000}
""",
        encoding="utf-8",
    )
    loaded = load_config(config)
    assert [camera.camera_id for camera in loaded.cameras] == ["one", "two"]


def test_rejects_duplicate_listener(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
cameras:
  - {id: one, driver: icsee_onvif, host: 10.0.0.1, listen: {port: 8999}}
  - {id: two, driver: icsee_onvif, host: 10.0.0.2, listen: {port: 8999}}
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate listener"):
        load_config(config)


def test_registry_rejects_unknown_driver() -> None:
    config = CameraConfig(camera_id="x", name="x", driver="missing", host="10.0.0.1")
    with pytest.raises(ValueError, match="unknown driver"):
        DriverRegistry.defaults().create(config)


def test_tapo_driver_does_not_authenticate_during_construction(monkeypatch) -> None:
    calls = []

    class RejectingTapo:
        def __init__(self, *_args, **_kwargs):
            calls.append("constructed")
            raise Exception("Invalid authentication data")

    monkeypatch.setitem(sys.modules, "pytapo", types.SimpleNamespace(Tapo=RejectingTapo))
    config = CameraConfig(
        camera_id="tapo",
        name="Tapo",
        driver="tapo_c200",
        host="192.0.2.10",
        username="camera-user",
        password="camera-password",
    )

    driver = TapoC200Driver(config)

    assert calls == []
    assert driver.status()["available"] is True
    with pytest.raises(RuntimeError, match="private API unavailable"):
        driver.move(1, 0, 0.1)
    assert calls == ["constructed"]
    status = driver.status()
    assert status["available"] is False
    assert "Invalid authentication data" in status["error"]


def test_runtime_keeps_other_camera_when_driver_creation_fails(monkeypatch) -> None:
    good = CameraConfig(
        camera_id="good",
        name="Good",
        driver="icsee_onvif",
        host="192.0.2.11",
        listen_port=12001,
    )
    bad = CameraConfig(
        camera_id="bad",
        name="Bad",
        driver="tapo_c200",
        host="192.0.2.12",
        listen_port=12002,
        username="u",
        password="p",
    )

    real_defaults = DriverRegistry.defaults

    class Registry:
        def create(self, camera):
            if camera.camera_id == "bad":
                raise RuntimeError("simulated failure")
            return real_defaults().create(camera)

    monkeypatch.setattr(DriverRegistry, "defaults", classmethod(lambda cls: Registry()))
    runtime = CameraRuntime(ProxyConfig(cameras=(good, bad)))

    assert "good" in runtime.drivers
    assert "bad" not in runtime.drivers
    assert "simulated failure" in runtime.errors["bad"]
    statuses = {item["id"]: item for item in runtime.status()}
    assert statuses["bad"]["available"] is False
    assert "simulated failure" in statuses["bad"]["error"]
