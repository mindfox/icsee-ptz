from __future__ import annotations

import json
import os
import subprocess
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth

from .config import ProxyConfig
from .registry import DriverRegistry

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _nodes(root: ET.Element, name: str):
    return [node for node in root.iter() if _local_name(node.tag) == name]


def _soap_action(body: bytes) -> str:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "unknown"
    bodies = _nodes(root, "Body")
    return _local_name(list(bodies[0])[0].tag) if bodies and list(bodies[0]) else "unknown"


def _pan_tilt(body: bytes) -> tuple[float, float]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return 0.0, 0.0
    values = _nodes(root, "PanTilt")
    if not values:
        return 0.0, 0.0
    try:
        return float(values[0].attrib.get("x", 0)), float(values[0].attrib.get("y", 0))
    except ValueError:
        return 0.0, 0.0


def _empty_response(operation: str) -> bytes:
    envelope = ET.Element(f"{{{SOAP12}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP12}}}Body")
    ET.SubElement(body, f"{{{TPTZ}}}{operation}Response")
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


class TapoOnvifHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status: int, payload: bytes, content_type: str = "application/soap+xml; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, b'{"status":"ok"}', "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        operation = _soap_action(body)
        if operation in {"RelativeMove", "ContinuousMove"}:
            pan, tilt = _pan_tilt(body)
            self.server.driver.move(pan, tilt, self.server.pulse_seconds)
            self._send(200, _empty_response(operation))
            return
        if operation == "Stop":
            self.server.driver.stop()
            self._send(200, _empty_response(operation))
            return

        upstream = f"http://{self.server.camera_config.host}:{self.server.onvif_port}{urlsplit(self.path).path}"
        headers = {key: value for key, value in self.headers.items() if key.lower() not in {"host", "content-length", "connection"}}
        try:
            response = requests.post(
                upstream,
                data=body,
                headers=headers,
                auth=HTTPDigestAuth(self.server.camera_config.username or "", self.server.camera_config.password or ""),
                timeout=self.server.timeout,
            )
            public_origin = f'http://{self.headers.get("Host", f"127.0.0.1:{self.server.server_port}")}'
            camera_origin = f"http://{self.server.camera_config.host}:{self.server.onvif_port}"
            payload = response.content.replace(camera_origin.encode(), public_origin.encode())
            self._send(response.status_code, payload, response.headers.get("Content-Type", "application/soap+xml; charset=utf-8"))
        except requests.RequestException as exc:
            self._send(502, str(exc).encode(), "text/plain")

    def log_message(self, fmt: str, *args) -> None:
        return


class TapoOnvifServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, camera_config, driver):
        super().__init__((camera_config.listen_host, camera_config.listen_port), TapoOnvifHandler)
        self.camera_config = camera_config
        self.driver = driver
        self.onvif_port = int(camera_config.options.get("onvif_port", 2020))
        self.timeout = float(camera_config.options.get("timeout", 10))
        self.pulse_seconds = float(camera_config.options.get("pulse_seconds", 0.1))


class CameraRuntime:
    def __init__(self, config: ProxyConfig, proxy_script: str = "proxy.py"):
        self.config = config
        self.proxy_script = proxy_script
        registry = DriverRegistry.defaults()
        self.drivers = {camera.camera_id: registry.create(camera) for camera in config.cameras}
        self.servers = []
        self.processes = []

    def start(self) -> None:
        for camera in self.config.cameras:
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
                self.processes.append(subprocess.Popen(["python", self.proxy_script], env=env))
            elif camera.driver == "tapo_c200":
                server = TapoOnvifServer(camera, self.drivers[camera.camera_id])
                threading.Thread(target=server.serve_forever, daemon=True).start()
                self.servers.append(server)
            else:
                raise ValueError(f"unsupported driver: {camera.driver}")

    def stop(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for process in self.processes:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()

    def status(self):
        result = []
        for camera in self.config.cameras:
            try:
                result.append(self.drivers[camera.camera_id].status())
            except Exception as exc:
                result.append({"id": camera.camera_id, "name": camera.name, "driver": camera.driver, "host": camera.host, "error": f"{type(exc).__name__}: {exc}"})
        return result

    def move(self, camera_id: str, pan: float, tilt: float, duration: float = 0.1) -> None:
        self.drivers[camera_id].move(pan, tilt, duration)

    def stop_camera(self, camera_id: str) -> None:
        self.drivers[camera_id].stop()


class WebHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, data, content_type: str = "application/json") -> None:
        payload = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/api/cameras":
            self._send(200, self.server.runtime.status())
        elif self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/":
            self._send(200, HTML.encode(), "text/html; charset=utf-8")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "cameras"]:
            self._send(404, {"error": "not found"})
            return
        camera_id, command = parts[2], parts[3]
        try:
            if command == "move":
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                self.server.runtime.move(camera_id, float(payload.get("pan", 0)), float(payload.get("tilt", 0)), float(payload.get("duration", 0.1)))
            elif command == "stop":
                self.server.runtime.stop_camera(camera_id)
            else:
                self._send(404, {"error": "unknown command"})
                return
            self._send(200, {"status": "ok"})
        except KeyError:
            self._send(404, {"error": "unknown camera"})
        except Exception as exc:
            self._send(502, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        return


HTML = '''<!doctype html><meta charset="utf-8"><title>Camera proxy</title><style>body{font:16px sans-serif;max-width:900px;margin:2rem auto}section{border:1px solid #aaa;padding:1rem;margin:1rem 0}button{font-size:1.2rem;margin:.2rem;padding:.5rem 1rem}</style><h1>Multi-camera ONVIF proxy</h1><main></main><script>async function post(id,cmd,p={}){await fetch(`/api/cameras/${id}/${cmd}`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(p)})}async function load(){let cs=await(await fetch('/api/cameras')).json();document.querySelector('main').innerHTML=cs.map(c=>`<section><h2>${c.name}</h2><div>${c.driver} — ${c.host}</div><button onclick="post('${c.id}','move',{tilt:1})">↑</button><br><button onclick="post('${c.id}','move',{pan:-1})">←</button><button onclick="post('${c.id}','stop')">■</button><button onclick="post('${c.id}','move',{pan:1})">→</button><br><button onclick="post('${c.id}','move',{tilt:-1})">↓</button><pre>${JSON.stringify(c.capabilities||c.error,null,2)}</pre></section>`).join('')}load()</script>'''


def start_web(runtime: CameraRuntime):
    server = ThreadingHTTPServer((runtime.config.web_host, runtime.config.web_port), WebHandler)
    server.runtime = runtime
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
