from __future__ import annotations

import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth

from .onvif_synth import advertise_ptz, synthetic_ptz_response

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
