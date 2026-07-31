from pathlib import Path

import pytest

from multicam.config import CameraConfig, load_config
from multicam.registry import DriverRegistry


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
