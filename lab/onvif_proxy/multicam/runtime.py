from __future__ import annotations

import os
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone

from .config import ProxyConfig
from .control import DvripSnapshotClient, ProxyOnvifClient, RtspSnapshotClient
from .registry import DriverRegistry
from .tapo_server import TapoServer


class CameraRuntime:
    def __init__(self, config: ProxyConfig, proxy_script="proxy.py"):
        self.config = config
        self.proxy_script = proxy_script
        self.registry = DriverRegistry.defaults()
        self.drivers = {}
        self.errors = {}
        self.servers = []
        self.processes = []
        self.controls = {}
        self.snapshots = {}
        self.feed_enabled = {}
        self.logs = {camera.camera_id: deque(maxlen=500) for camera in config.cameras}
        self.log_lock = threading.Lock()

        for camera in config.cameras:
            try:
                driver = self.registry.create(camera)
                self.drivers[camera.camera_id] = driver
                self.feed_enabled[camera.camera_id] = False
                if camera.driver == "icsee_onvif":
                    self.controls[camera.camera_id] = ProxyOnvifClient(camera)
                    if bool(camera.options.get("live_feed", True)):
                        self.snapshots[camera.camera_id] = DvripSnapshotClient(camera)
                elif camera.driver == "tapo_c200":
                    self.controls[camera.camera_id] = driver
                    if bool(camera.options.get("live_feed", True)):
                        self.snapshots[camera.camera_id] = RtspSnapshotClient(camera)
                self.add_log(camera.camera_id, "INFO", f"Configured driver={camera.driver} host={camera.host} listener={camera.listen_host}:{camera.listen_port}")
            except Exception as exc:
                self.errors[camera.camera_id] = f"{type(exc).__name__}: {exc}"
                self.add_log(camera.camera_id, "ERROR", f"Driver initialization failed: {self.errors[camera.camera_id]}")

    def add_log(self, camera_id, level, message):
        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")
        entry = {"timestamp": timestamp, "level": level, "message": str(message)}
        with self.log_lock:
            self.logs.setdefault(camera_id, deque(maxlen=500)).append(entry)
        print(f"[{timestamp}] [{level}] camera={camera_id} {message}", flush=True)

    def get_logs(self, camera_id):
        self._camera(camera_id)
        with self.log_lock:
            return list(self.logs.get(camera_id, ()))

    def _capture_process_output(self, camera_id, process):
        if process.stdout is None:
            return
        for line in process.stdout:
            text = line.rstrip()
            if text:
                self.add_log(camera_id, "PROXY", text)

    def _camera(self, camera_id):
        for camera in self.config.cameras:
            if camera.camera_id == camera_id:
                return camera
        raise KeyError(camera_id)

    def start(self):
        for camera in self.config.cameras:
            if camera.camera_id in self.errors:
                continue
            try:
                if camera.driver == "icsee_onvif":
                    env = os.environ.copy()
                    env.update({
                        "CAMERA_HOST": camera.host,
                        "CAMERA_ONVIF_PORT": str(camera.options.get("onvif_port", 8899)),
                        "CAMERA_USERNAME": camera.username or "",
                        "CAMERA_PASSWORD": camera.password or "",
                        "PROXY_LISTEN_HOST": camera.listen_host,
                        "PROXY_LISTEN_PORT": str(camera.listen_port),
                    })
                    process = subprocess.Popen(
                        ["python", self.proxy_script],
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                    self.processes.append((camera.camera_id, process))
                    threading.Thread(target=self._capture_process_output, args=(camera.camera_id, process), daemon=True).start()
                    self.add_log(camera.camera_id, "INFO", f"Proxy process started pid={process.pid}")
                elif camera.driver == "tapo_c200":
                    server = TapoServer(camera, self.drivers[camera.camera_id], self)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    self.servers.append((camera.camera_id, server))
                    self.add_log(camera.camera_id, "INFO", "Tapo ONVIF listener started")
                else:
                    raise ValueError(f"unsupported driver: {camera.driver}")
            except Exception as exc:
                self.errors[camera.camera_id] = f"{type(exc).__name__}: {exc}"
                self.add_log(camera.camera_id, "ERROR", f"Startup failed: {self.errors[camera.camera_id]}")

    def stop(self):
        for camera_id, server in self.servers:
            self.add_log(camera_id, "INFO", "Stopping listener")
            server.shutdown()
            server.server_close()
        for camera_id, process in self.processes:
            self.add_log(camera_id, "INFO", "Stopping proxy process")
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()

    def status(self):
        process_status = {camera_id: process.poll() is None for camera_id, process in self.processes}
        result = []
        for camera in self.config.cameras:
            if camera.camera_id in self.errors:
                result.append({"id": camera.camera_id, "name": camera.name, "driver": camera.driver, "host": camera.host, "available": False, "error": self.errors[camera.camera_id]})
                continue
            try:
                item = self.drivers[camera.camera_id].status()
                if camera.driver == "icsee_onvif" and not process_status.get(camera.camera_id, False):
                    item["available"] = False
                    item["error"] = "legacy proxy process is not running"
                item["feed_supported"] = camera.camera_id in self.snapshots
                item["feed_enabled"] = self.feed_enabled.get(camera.camera_id, False)
                result.append(item)
            except Exception as exc:
                result.append({"id": camera.camera_id, "name": camera.name, "driver": camera.driver, "host": camera.host, "available": False, "error": f"{type(exc).__name__}: {exc}"})
        return result

    def _driver(self, camera_id):
        if camera_id in self.errors:
            raise RuntimeError(self.errors[camera_id])
        return self.drivers[camera_id]

    def _control(self, camera_id):
        try:
            return self.controls[camera_id]
        except KeyError as exc:
            raise RuntimeError(f"{camera_id}: control is not supported") from exc

    def diagnostics(self, camera_id):
        self.add_log(camera_id, "INFO", "Read-only ONVIF diagnostics requested")
        result = self._control(camera_id).diagnostics()
        self.add_log(camera_id, "INFO", "Read-only ONVIF diagnostics completed")
        return result

    def ptz_status(self, camera_id):
        control = self._control(camera_id)
        function = getattr(control, "ptz_status", None) or getattr(control, "get_status", None)
        if function is None:
            raise RuntimeError(f"{camera_id}: PTZ status is not supported")
        result = function()
        self.add_log(camera_id, "INFO", f"PTZ status: {result!r}")
        return result

    def move(self, camera_id, pan, tilt, duration=0.1):
        self.add_log(camera_id, "INFO", f"PTZ move requested pan={pan:g} tilt={tilt:g} duration={duration:g}s")
        self._driver(camera_id).move(pan, tilt, duration)
        self.add_log(camera_id, "INFO", "PTZ move accepted")

    def stop_camera(self, camera_id):
        self.add_log(camera_id, "INFO", "PTZ stop requested")
        self._driver(camera_id).stop()
        self.add_log(camera_id, "INFO", "PTZ stop accepted")

    def zoom(self, camera_id, direction, duration=0.08):
        velocity = 0.5 if direction == "in" else -0.5 if direction == "out" else None
        if velocity is None:
            raise ValueError("zoom direction must be in or out")
        self.add_log(camera_id, "INFO", f"Zoom {direction} requested duration={duration:g}s")
        result = self._control(camera_id).continuous_move(zoom=velocity, seconds=duration)
        self.add_log(camera_id, "INFO", f"Zoom {direction} response: {result!r}")
        return result

    def presets(self, camera_id):
        self.add_log(camera_id, "INFO", "ONVIF GetPresets requested")
        result = self._control(camera_id).get_presets() if hasattr(self._control(camera_id), "get_presets") else self._control(camera_id).presets()
        self.add_log(camera_id, "INFO", f"ONVIF GetPresets returned count={len(result)}")
        return result

    def set_preset(self, camera_id, preset_token, name):
        if len(name) > 40:
            raise ValueError("preset name must be at most 40 characters")
        self.add_log(camera_id, "INFO", f"ONVIF SetPreset token={preset_token!r} name={name!r}")
        result = self._control(camera_id).set_preset(preset_token, name)
        self.add_log(camera_id, "INFO", f"ONVIF SetPreset response: {result!r}")
        return result

    def goto_preset(self, camera_id, preset_token, speed_x=1, speed_y=1):
        control = self._control(camera_id)
        self.add_log(camera_id, "INFO", f"ONVIF GotoPreset token={preset_token!r}")
        if self._camera(camera_id).driver == "tapo_c200":
            result = control.goto_preset(preset_token)
        else:
            if not 1 <= speed_x <= 8 or not 1 <= speed_y <= 8:
                raise ValueError("preset speeds must be from 1 to 8")
            result = control.goto_preset(preset_token, speed_x, speed_y)
        self.add_log(camera_id, "INFO", f"ONVIF GotoPreset response: {result!r}")
        return result

    def remove_preset(self, camera_id, preset_token):
        control = self._control(camera_id)
        if not hasattr(control, "remove_preset"):
            raise RuntimeError(f"{camera_id}: removing presets is not supported")
        self.add_log(camera_id, "INFO", f"ONVIF RemovePreset token={preset_token!r}")
        result = control.remove_preset(preset_token)
        self.add_log(camera_id, "INFO", f"ONVIF RemovePreset response: {result!r}")
        return result

    def goto_home(self, camera_id):
        control = self._control(camera_id)
        if not hasattr(control, "goto_home"):
            raise RuntimeError(f"{camera_id}: home position is not supported")
        self.add_log(camera_id, "INFO", "ONVIF GotoHomePosition requested")
        return control.goto_home()

    def set_home(self, camera_id):
        control = self._control(camera_id)
        if not hasattr(control, "set_home"):
            raise RuntimeError(f"{camera_id}: setting home position is not supported")
        self.add_log(camera_id, "INFO", "ONVIF SetHomePosition requested")
        return control.set_home()

    def set_feed(self, camera_id, enabled):
        self._camera(camera_id)
        if enabled and camera_id not in self.snapshots:
            raise RuntimeError(f"{camera_id}: live feed is not supported")
        self.feed_enabled[camera_id] = bool(enabled)
        self.add_log(camera_id, "INFO", f"Live feed {'enabled' if enabled else 'disabled'}")
        return self.feed_enabled[camera_id]

    def snapshot(self, camera_id):
        self._camera(camera_id)
        if not self.feed_enabled.get(camera_id, False):
            raise RuntimeError("live feed is disabled")
        try:
            client = self.snapshots[camera_id]
        except KeyError as exc:
            raise RuntimeError(f"{camera_id}: live feed is not supported") from exc
        return client.snapshot()
