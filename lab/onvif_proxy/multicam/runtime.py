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
                self.server.driver.move(pan, tilt, self.server.pulse)
                return self.send_payload(200, empty(operation))
            if operation == "Stop":
                self.server.driver.stop()
                return self.send_payload(200, empty(operation))
        except Exception as exc:
            return self.send_payload(
                503,
                f"{type(exc).__name__}: {exc}".encode(),
                "text/plain; charset=utf-8",
            )

        synthesized = (
            synthetic_ptz_response(operation)
            if urlsplit(self.path).path.endswith("ptz_service")
            else None
        )
        if synthesized is not None:
            return self.send_payload(200, synthesized)

        upstream = (
            f"http://{self.server.camera.host}:{self.server.onvif_port}"
            f"{urlsplit(self.path).path}"
        )
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
        try:
            response = requests.post(
                upstream,
                data=body,
                headers=headers,
                auth=HTTPDigestAuth(
                    self.server.camera.username or "",
                    self.server.camera.password or "",
                ),
                timeout=self.server.timeout,
            )
            public = f'http://{self.headers.get("Host", f"127.0.0.1:{self.server.server_port}")}'
            camera = f"http://{self.server.camera.host}:{self.server.onvif_port}"
            payload = response.content.replace(camera.encode(), public.encode())
            if operation in {"GetCapabilities", "GetServices"}:
                payload = advertise_ptz(payload, public)
            self.send_payload(
                response.status_code,
                payload,
                response.headers.get("Content-Type", "application/soap+xml; charset=utf-8"),
            )
        except requests.RequestException as exc:
            self.send_payload(502, str(exc).encode(), "text/plain; charset=utf-8")

    def log_message(self, *_):
        return


class TapoServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, camera, driver):
        super().__init__((camera.listen_host, camera.listen_port), TapoHandler)
        self.camera = camera
        self.driver = driver
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

        for camera in config.cameras:
            try:
                self.drivers[camera.camera_id] = self.registry.create(camera)
            except Exception as exc:
                self.errors[camera.camera_id] = f"{type(exc).__name__}: {exc}"

    def start(self):
        for camera in self.config.cameras:
            if camera.camera_id in self.errors:
                continue
            try:
                if camera.driver == "icsee_onvif":
                    env = os.environ.copy()
                    env.update(
                        {
                            "CAMERA_HOST": camera.host,
                            "CAMERA_ONVIF_PORT": str(camera.options.get("onvif_port", 8899)),
                            "CAMERA_USERNAME": camera.username or "",
                            "CAMERA_PASSWORD": camera.password or "",
                            "PROXY_LISTEN_HOST": camera.listen_host,
                            "PROXY_LISTEN_PORT": str(camera.listen_port),
                        }
                    )
                    process = subprocess.Popen(["python", self.proxy_script], env=env)
                    self.processes.append((camera.camera_id, process))
                elif camera.driver == "tapo_c200":
                    server = TapoServer(camera, self.drivers[camera.camera_id])
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    self.servers.append((camera.camera_id, server))
                else:
                    raise ValueError(f"unsupported driver: {camera.driver}")
            except Exception as exc:
                self.errors[camera.camera_id] = f"{type(exc).__name__}: {exc}"

    def stop(self):
        for _, server in self.servers:
            server.shutdown()
            server.server_close()
        for _, process in self.processes:
            process.terminate()
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()

    def status(self):
        process_status = {
            camera_id: process.poll() is None for camera_id, process in self.processes
        }
        result = []
        for camera in self.config.cameras:
            if camera.camera_id in self.errors:
                result.append(
                    {
                        "id": camera.camera_id,
                        "name": camera.name,
                        "driver": camera.driver,
                        "host": camera.host,
                        "available": False,
                        "error": self.errors[camera.camera_id],
                    }
                )
                continue
            try:
                item = self.drivers[camera.camera_id].status()
                if camera.driver == "icsee_onvif" and not process_status.get(camera.camera_id, False):
                    item["available"] = False
                    item["error"] = "legacy proxy process is not running"
                result.append(item)
            except Exception as exc:
                result.append(
                    {
                        "id": camera.camera_id,
                        "name": camera.name,
                        "driver": camera.driver,
                        "host": camera.host,
                        "available": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        return result

    def _driver(self, camera_id):
        if camera_id in self.errors:
            raise RuntimeError(self.errors[camera_id])
        return self.drivers[camera_id]

    def move(self, camera_id, pan, tilt, duration=0.1):
        self._driver(camera_id).move(pan, tilt, duration)

    def stop_camera(self, camera_id):
        self._driver(camera_id).stop()


class WebHandler(BaseHTTPRequestHandler):
    def send_payload(self, status, data, content_type="application/json"):
        payload = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/api/cameras":
            self.send_payload(200, self.server.runtime.status())
        elif self.path == "/health":
            self.send_payload(200, {"status": "ok"})
        elif self.path == "/":
            self.send_payload(200, HTML.encode(), "text/html; charset=utf-8")
        else:
            self.send_payload(404, {"error": "not found"})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "cameras"]:
            return self.send_payload(404, {"error": "not found"})
        camera_id, command = parts[2], parts[3]
        try:
            if command == "move":
                payload = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
                )
                self.server.runtime.move(
                    camera_id,
                    float(payload.get("pan", 0)),
                    float(payload.get("tilt", 0)),
                    float(payload.get("duration", 0.1)),
                )
            elif command == "stop":
                self.server.runtime.stop_camera(camera_id)
            else:
                return self.send_payload(404, {"error": "unknown command"})
            self.send_payload(200, {"status": "ok"})
        except KeyError:
            self.send_payload(404, {"error": "unknown camera"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_payload(400, {"error": str(exc)})
        except Exception as exc:
            self.send_payload(503, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, *_):
        return


HTML = '''<!doctype html><meta charset="utf-8"><title>Camera proxy</title><style>body{font:16px sans-serif;max-width:900px;margin:2rem auto}section{border:1px solid #aaa;padding:1rem;margin:1rem 0}button{font-size:1.2rem;margin:.2rem;padding:.5rem 1rem}</style><h1>Multi-camera ONVIF proxy</h1><main></main><script>async function post(id,cmd,p={}){let r=await fetch(`/api/cameras/${id}/${cmd}`,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(p)});if(!r.ok)alert(await r.text());await load()}async function load(){let cs=await(await fetch('/api/cameras')).json();document.querySelector('main').innerHTML=cs.map(c=>`<section><h2>${c.name}</h2><div>${c.driver} — ${c.host}</div><div>Status: ${c.available===false?'unavailable':'available'}</div><button onclick="post('${c.id}','move',{tilt:1})">↑</button><br><button onclick="post('${c.id}','move',{pan:-1})">←</button><button onclick="post('${c.id}','stop')">■</button><button onclick="post('${c.id}','move',{pan:1})">→</button><br><button onclick="post('${c.id}','move',{tilt:-1})">↓</button><pre>${JSON.stringify(c.error||c.capabilities,null,2)}</pre></section>`).join('')}load()</script>'''


def start_web(runtime):
    server = ThreadingHTTPServer(
        (runtime.config.web_host, runtime.config.web_port), WebHandler
    )
    server.runtime = runtime
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
