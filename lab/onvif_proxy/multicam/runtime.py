from __future__ import annotations

import json
import os
import subprocess
import threading
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import requests
from requests.auth import HTTPDigestAuth

from .config import ProxyConfig
from .control import DvripSnapshotClient, ProxyOnvifClient
from .onvif_synth import advertise_ptz, synthetic_ptz_response
from .registry import DriverRegistry

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"


def lname(tag):
    return tag.rsplit("}", 1)[-1]


def action(body):
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "unknown"
    bodies = [node for node in root.iter() if lname(node.tag) == "Body"]
    return lname(list(bodies[0])[0].tag) if bodies and list(bodies[0]) else "unknown"


def vector(body):
    try:
        root = ET.fromstring(body)
        points = [node for node in root.iter() if lname(node.tag) == "PanTilt"]
    except ET.ParseError:
        return 0.0, 0.0
    if not points:
        return 0.0, 0.0
    try:
        return float(points[0].attrib.get("x", 0)), float(points[0].attrib.get("y", 0))
    except ValueError:
        return 0.0, 0.0


def empty(operation):
    envelope = ET.Element(f"{{{SOAP12}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP12}}}Body")
    ET.SubElement(body, f"{{{TPTZ}}}{operation}Response")
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


class TapoHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_payload(self, status, payload, content_type="application/soap+xml; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/health":
            self.send_payload(200, b'{"status":"ok"}', "application/json")
        else:
            self.send_payload(404, b"not found", "text/plain")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        operation = action(body)
        try:
            if operation in {"RelativeMove", "ContinuousMove"}:
                pan, tilt = vector(body)
                self.server.runtime.add_log(self.server.camera.camera_id, "INFO", f"ONVIF {operation}: pan={pan:g} tilt={tilt:g}")
                self.server.driver.move(pan, tilt, self.server.pulse)
                return self.send_payload(200, empty(operation))
            if operation == "Stop":
                self.server.runtime.add_log(self.server.camera.camera_id, "INFO", "ONVIF Stop")
                self.server.driver.stop()
                return self.send_payload(200, empty(operation))
        except Exception as exc:
            self.server.runtime.add_log(self.server.camera.camera_id, "ERROR", f"{operation} failed: {type(exc).__name__}: {exc}")
            return self.send_payload(503, f"{type(exc).__name__}: {exc}".encode(), "text/plain; charset=utf-8")

        synthesized = synthetic_ptz_response(operation) if urlsplit(self.path).path.endswith("ptz_service") else None
        if synthesized is not None:
            return self.send_payload(200, synthesized)

        upstream = f"http://{self.server.camera.host}:{self.server.onvif_port}{urlsplit(self.path).path}"
        headers = {key: value for key, value in self.headers.items() if key.lower() not in {"host", "content-length", "connection"}}
        try:
            response = requests.post(
                upstream,
                data=body,
                headers=headers,
                auth=HTTPDigestAuth(self.server.camera.username or "", self.server.camera.password or ""),
                timeout=self.server.timeout,
            )
            public = f'http://{self.headers.get("Host", f"127.0.0.1:{self.server.server_port}")}'
            camera = f"http://{self.server.camera.host}:{self.server.onvif_port}"
            payload = response.content.replace(camera.encode(), public.encode())
            if operation in {"GetCapabilities", "GetServices"}:
                payload = advertise_ptz(payload, public)
            self.send_payload(response.status_code, payload, response.headers.get("Content-Type", "application/soap+xml; charset=utf-8"))
        except requests.RequestException as exc:
            self.server.runtime.add_log(self.server.camera.camera_id, "ERROR", f"ONVIF forward failed: {type(exc).__name__}: {exc}")
            self.send_payload(502, str(exc).encode(), "text/plain; charset=utf-8")

    def log_message(self, *_):
        return


class TapoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, camera, driver, runtime):
        super().__init__((camera.listen_host, camera.listen_port), TapoHandler)
        self.camera = camera
        self.driver = driver
        self.runtime = runtime
        self.onvif_port = int(camera.options.get("onvif_port", 2020))
        self.timeout = float(camera.options.get("timeout", 10))
        self.pulse = float(camera.options.get("pulse_seconds", 0.1))


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
                self.drivers[camera.camera_id] = self.registry.create(camera)
                self.feed_enabled[camera.camera_id] = False
                if camera.driver == "icsee_onvif":
                    self.controls[camera.camera_id] = ProxyOnvifClient(camera)
                    if bool(camera.options.get("live_feed", True)):
                        self.snapshots[camera.camera_id] = DvripSnapshotClient(camera)
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
        result = self._control(camera_id).get_presets()
        self.add_log(camera_id, "INFO", f"ONVIF GetPresets returned count={len(result)}")
        for preset in result:
            self.add_log(camera_id, "INFO", f"Preset: {preset!r}")
        return result

    def set_preset(self, camera_id, preset_token, name):
        if len(name) > 40:
            raise ValueError("preset name must be at most 40 characters")
        self.add_log(camera_id, "INFO", f"ONVIF SetPreset token={preset_token!r} name={name!r}")
        result = self._control(camera_id).set_preset(preset_token, name)
        self.add_log(camera_id, "INFO", f"ONVIF SetPreset response: {result!r}")
        return result

    def goto_preset(self, camera_id, preset_token, speed_x=1, speed_y=1):
        if not 1 <= speed_x <= 8 or not 1 <= speed_y <= 8:
            raise ValueError("preset speeds must be from 1 to 8")
        self.add_log(camera_id, "INFO", f"ONVIF GotoPreset token={preset_token!r} speed=({speed_x},{speed_y})")
        result = self._control(camera_id).goto_preset(preset_token, speed_x, speed_y)
        self.add_log(camera_id, "INFO", f"ONVIF GotoPreset response: {result!r}")
        return result

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


class WebHandler(BaseHTTPRequestHandler):
    def send_payload(self, status, data, content_type="application/json"):
        payload = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        try:
            if path == "/api/cameras":
                return self.send_payload(200, self.server.runtime.status())
            if len(parts) == 4 and parts[:2] == ["api", "cameras"] and parts[3] == "logs":
                return self.send_payload(200, {"entries": self.server.runtime.get_logs(unquote(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "cameras"] and parts[3] == "presets":
                return self.send_payload(200, {"presets": self.server.runtime.presets(unquote(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "cameras"] and parts[3] == "snapshot.jpg":
                return self.send_payload(200, self.server.runtime.snapshot(unquote(parts[2])), "image/jpeg")
            if path == "/health":
                return self.send_payload(200, {"status": "ok"})
            if path == "/":
                return self.send_payload(200, HTML.encode(), "text/html; charset=utf-8")
            return self.send_payload(404, {"error": "not found"})
        except KeyError:
            self.send_payload(404, {"error": "unknown camera"})
        except Exception as exc:
            self.send_payload(503, {"error": f"{type(exc).__name__}: {exc}"})

    def do_POST(self):
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[:2] != ["api", "cameras"]:
            return self.send_payload(404, {"error": "not found"})
        camera_id, command = unquote(parts[2]), parts[3]
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            result = None
            if command == "move" and len(parts) == 4:
                self.server.runtime.move(camera_id, float(payload.get("pan", 0)), float(payload.get("tilt", 0)), float(payload.get("duration", 0.1)))
            elif command == "stop" and len(parts) == 4:
                self.server.runtime.stop_camera(camera_id)
            elif command == "zoom" and len(parts) == 4:
                result = self.server.runtime.zoom(camera_id, str(payload.get("direction", "")), float(payload.get("duration", 0.08)))
            elif command == "feed" and len(parts) == 4:
                result = {"enabled": self.server.runtime.set_feed(camera_id, bool(payload.get("enabled", False)))}
            elif command == "presets" and len(parts) == 5:
                result = self.server.runtime.set_preset(camera_id, unquote(parts[4]), str(payload.get("name", "")).strip())
            elif command == "presets" and len(parts) == 6 and parts[5] == "goto":
                result = self.server.runtime.goto_preset(camera_id, unquote(parts[4]), int(payload.get("speed_x", 1)), int(payload.get("speed_y", 1)))
            else:
                return self.send_payload(404, {"error": "unknown command"})
            self.send_payload(200, {"status": "ok", "result": result})
        except KeyError:
            self.send_payload(404, {"error": "unknown camera"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_payload(400, {"error": str(exc)})
        except Exception as exc:
            self.server.runtime.add_log(camera_id, "ERROR", f"Web command failed: {type(exc).__name__}: {exc}")
            self.send_payload(503, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, *_):
        return


HTML = "<!doctype html><meta charset='utf-8'><title>Camera proxy</title>"


def start_web(runtime):
    server = ThreadingHTTPServer((runtime.config.web_host, runtime.config.web_port), WebHandler)
    server.runtime = runtime
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
