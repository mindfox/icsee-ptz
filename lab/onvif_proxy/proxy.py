import math
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests
from requests.auth import HTTPDigestAuth

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"
FOV_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"
GENERIC_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"

ET.register_namespace("s", SOAP12)
ET.register_namespace("tptz", TPTZ)
ET.register_namespace("tt", TT)

CAMERA_HOST = os.environ["CAMERA_HOST"]
CAMERA_ONVIF_PORT = int(os.environ.get("CAMERA_ONVIF_PORT", "8899"))
CAMERA_USERNAME = os.environ["CAMERA_USERNAME"]
CAMERA_PASSWORD = os.environ["CAMERA_PASSWORD"]
LISTEN_HOST = os.environ.get("PROXY_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("PROXY_LISTEN_PORT", "8999"))
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "10"))
CONTINUOUS_VELOCITY = float(os.environ.get("CONTINUOUS_VELOCITY", "0.5"))
PULSE_MIN_SECONDS = float(os.environ.get("PULSE_MIN_SECONDS", "0.04"))
PULSE_SECONDS_PER_FOV = float(os.environ.get("PULSE_SECONDS_PER_FOV", "0.80"))
PULSE_MAX_SECONDS = float(os.environ.get("PULSE_MAX_SECONDS", "1.00"))
STATUS_SETTLE_SECONDS = float(os.environ.get("STATUS_SETTLE_SECONDS", "0.20"))

UPSTREAM_ORIGIN = f"http://{CAMERA_HOST}:{CAMERA_ONVIF_PORT}"
_state_lock = threading.Lock()
_synthetic_moving_until = 0.0
_move_generation = 0


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] [ONVIF-PROXY] {message}", flush=True)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_all(root: ET.Element, name: str):
    return [node for node in root.iter() if local_name(node.tag) == name]


def parse_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def public_origin(handler: BaseHTTPRequestHandler) -> str:
    host = handler.headers.get("Host", "").strip() or f"127.0.0.1:{LISTEN_PORT}"
    return f"http://{host}"


def upstream_headers(action: str) -> dict[str, str]:
    return {
        "Content-Type": f'application/soap+xml; charset=utf-8; action="http://www.onvif.org/ver20/ptz/wsdl/{action}"',
        "Connection": "close",
    }


def upstream_post(path: str, action: str, payload: bytes) -> requests.Response:
    return requests.post(
        f"{UPSTREAM_ORIGIN}{path}",
        data=payload,
        headers=upstream_headers(action),
        auth=HTTPDigestAuth(CAMERA_USERNAME, CAMERA_PASSWORD),
        timeout=UPSTREAM_TIMEOUT,
    )


def soap_payload(operation: str, profile_token: str, *, x: float = 0.0, y: float = 0.0) -> bytes:
    envelope = ET.Element(f"{{{SOAP12}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP12}}}Body")
    op = ET.SubElement(body, f"{{{TPTZ}}}{operation}")
    ET.SubElement(op, f"{{{TPTZ}}}ProfileToken").text = profile_token
    if operation == "ContinuousMove":
        velocity = ET.SubElement(op, f"{{{TPTZ}}}Velocity")
        pan_tilt = ET.SubElement(velocity, f"{{{TT}}}PanTilt")
        pan_tilt.set("x", f"{x:g}")
        pan_tilt.set("y", f"{y:g}")
    elif operation == "Stop":
        ET.SubElement(op, f"{{{TPTZ}}}PanTilt").text = "true"
        ET.SubElement(op, f"{{{TPTZ}}}Zoom").text = "false"
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def relative_move_response() -> bytes:
    envelope = ET.Element(f"{{{SOAP12}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP12}}}Body")
    ET.SubElement(body, f"{{{TPTZ}}}RelativeMoveResponse")
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def set_moving_for(seconds: float) -> int:
    global _synthetic_moving_until, _move_generation
    with _state_lock:
        _move_generation += 1
        generation = _move_generation
        _synthetic_moving_until = time.monotonic() + seconds + STATUS_SETTLE_SECONDS
        return generation


def clear_synthetic_move(generation: int | None = None) -> None:
    global _synthetic_moving_until
    with _state_lock:
        if generation is None or generation == _move_generation:
            _synthetic_moving_until = 0.0


def synthetic_is_moving() -> bool:
    with _state_lock:
        return time.monotonic() < _synthetic_moving_until


def pulse_duration(x: float, y: float) -> float:
    magnitude = max(abs(x), abs(y))
    if magnitude <= 1e-9:
        return 0.0
    return max(PULSE_MIN_SECONDS, min(PULSE_MAX_SECONDS, PULSE_MIN_SECONDS + magnitude * PULSE_SECONDS_PER_FOV))


def extract_relative_move(body: bytes) -> dict | None:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    body_nodes = find_all(root, "Body")
    if not body_nodes or not list(body_nodes[0]) or local_name(list(body_nodes[0])[0].tag) != "RelativeMove":
        return None
    profile_nodes = find_all(root, "ProfileToken")
    if not profile_nodes or not (profile_nodes[0].text or "").strip():
        return None
    for pan_tilt in find_all(root, "PanTilt"):
        if pan_tilt.attrib.get("space") == FOV_SPACE:
            return {
                "profile_token": (profile_nodes[0].text or "").strip(),
                "x": parse_float(pan_tilt.attrib.get("x")),
                "y": parse_float(pan_tilt.attrib.get("y")),
            }
    return None


def stop_after_pulse(path: str, profile_token: str, delay: float, generation: int) -> None:
    time.sleep(delay)
    try:
        response = upstream_post(path, "Stop", soap_payload("Stop", profile_token))
        log(f"pulse-stop profile={profile_token!r} status={response.status_code} delay={delay:.3f}s")
    except Exception as exc:
        log(f"ERROR pulse-stop profile={profile_token!r}: {type(exc).__name__}: {exc}")
    finally:
        clear_synthetic_move(generation)


def ensure_fov_space(root: ET.Element) -> bool:
    changed = False
    for spaces in find_all(root, "Spaces") + find_all(root, "SupportedPTZSpaces"):
        existing = [node for node in list(spaces) if local_name(node.tag) == "RelativePanTiltTranslationSpace"]
        if not any((next((u.text for u in node if local_name(u.tag) == "URI"), "") or "").strip() == FOV_SPACE for node in existing):
            template = existing[0] if existing else None
            entry = ET.Element(f"{{{TT}}}RelativePanTiltTranslationSpace")
            ET.SubElement(entry, f"{{{TT}}}URI").text = FOV_SPACE
            x_range = ET.SubElement(entry, f"{{{TT}}}XRange")
            ET.SubElement(x_range, f"{{{TT}}}Min").text = "-1"
            ET.SubElement(x_range, f"{{{TT}}}Max").text = "1"
            y_range = ET.SubElement(entry, f"{{{TT}}}YRange")
            ET.SubElement(y_range, f"{{{TT}}}Min").text = "-1"
            ET.SubElement(y_range, f"{{{TT}}}Max").text = "1"
            if template is not None:
                spaces.insert(list(spaces).index(template) + 1, entry)
            else:
                spaces.append(entry)
            changed = True

    for node in find_all(root, "DefaultRelativePanTiltTranslationSpace"):
        if (node.text or "").strip() != FOV_SPACE:
            node.text = FOV_SPACE
            changed = True

    return changed


def ensure_move_status_capability(root: ET.Element) -> bool:
    changed = False
    for node in find_all(root, "Capabilities"):
        if node.attrib.get("MoveStatus", "").lower() != "true":
            node.set("MoveStatus", "true")
            changed = True
        if node.attrib.get("StatusPosition", "").lower() != "true":
            node.set("StatusPosition", "true")
            changed = True
    return changed


def rewrite_xaddrs(root: ET.Element, origin: str) -> bool:
    changed = False
    for node in root.iter():
        if not node.text:
            continue
        replaced = re.sub(rf"http://{re.escape(CAMERA_HOST)}(?::{CAMERA_ONVIF_PORT})?", origin, node.text)
        if replaced != node.text:
            node.text = replaced
            changed = True
    return changed


def synthesize_status(root: ET.Element) -> bool:
    if not synthetic_is_moving():
        return False
    changed = False
    for move_status in find_all(root, "MoveStatus"):
        pan_tilt_nodes = [node for node in list(move_status) if local_name(node.tag) == "PanTilt"]
        if pan_tilt_nodes:
            pan_tilt_nodes[0].text = "MOVING"
        else:
            ET.SubElement(move_status, f"{{{TT}}}PanTilt").text = "MOVING"
        changed = True
    return changed


def transform_response(body: bytes, origin: str, action: str) -> bytes:
    if not body:
        return body
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        text = body.decode("utf-8", errors="replace")
        return re.sub(rf"http://{re.escape(CAMERA_HOST)}(?::{CAMERA_ONVIF_PORT})?", origin, text).encode("utf-8")
    changed = rewrite_xaddrs(root, origin)
    if action in {"GetNode", "GetConfiguration", "GetConfigurationOptions"}:
        changed = ensure_fov_space(root) or changed
    if action == "GetServiceCapabilities":
        changed = ensure_move_status_capability(root) or changed
    if action == "GetStatus":
        changed = synthesize_status(root) or changed
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) if changed else body


def soap_action(body: bytes) -> str:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "unknown"
    body_nodes = find_all(root, "Body")
    return local_name(list(body_nodes[0])[0].tag) if body_nodes and list(body_nodes[0]) else "unknown"


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path == "/health":
            payload = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.forward()

    def do_POST(self):
        self.forward()

    def send_payload(self, status: int, payload: bytes, content_type: str = "application/soap+xml; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def forward(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        incoming = self.rfile.read(length) if length else b""
        path = urlsplit(self.path).path
        action = soap_action(incoming)
        relative = extract_relative_move(incoming)
        started = time.monotonic()

        try:
            if relative is not None:
                x = relative["x"]
                y = relative["y"]
                duration = pulse_duration(x, y)
                if duration <= 0:
                    self.send_payload(200, relative_move_response())
                    log(f"{self.client_address[0]} POST {path} action=RelativeMove translated=noop x={x:g} y={y:g}")
                    return
                scale = max(abs(x), abs(y))
                vx = 0.0 if abs(x) < 1e-9 else math.copysign(CONTINUOUS_VELOCITY * abs(x) / scale, x)
                vy = 0.0 if abs(y) < 1e-9 else math.copysign(CONTINUOUS_VELOCITY * abs(y) / scale, y)
                response = upstream_post(path, "ContinuousMove", soap_payload("ContinuousMove", relative["profile_token"], x=vx, y=vy))
                if response.status_code >= 400:
                    self.send_payload(response.status_code, response.content, response.headers.get("Content-Type", "application/soap+xml; charset=utf-8"))
                    log(f"ERROR RelativeMove->ContinuousMove upstream={response.status_code} x={x:g} y={y:g}")
                    return
                generation = set_moving_for(duration)
                threading.Thread(
                    target=stop_after_pulse,
                    args=(path, relative["profile_token"], duration, generation),
                    daemon=True,
                ).start()
                self.send_payload(200, relative_move_response())
                elapsed_ms = round((time.monotonic() - started) * 1000, 1)
                log(
                    f"{self.client_address[0]} POST {path} action=RelativeMove translated=ContinuousMovePulse "
                    f"requested=({x:g},{y:g}) velocity=({vx:g},{vy:g}) pulse_seconds={duration:.3f} "
                    f"upstream={response.status_code} elapsed_ms={elapsed_ms}"
                )
                return

            if action == "Stop":
                clear_synthetic_move()

            upstream_url = f"{UPSTREAM_ORIGIN}{path}"
            if urlsplit(self.path).query:
                upstream_url += f"?{urlsplit(self.path).query}"
            headers = {name: self.headers[name] for name in ("Content-Type", "SOAPAction", "Accept", "User-Agent") if name in self.headers}
            headers["Connection"] = "close"
            response = requests.request(
                self.command,
                upstream_url,
                data=incoming if self.command != "GET" else None,
                headers=headers,
                auth=HTTPDigestAuth(CAMERA_USERNAME, CAMERA_PASSWORD),
                timeout=UPSTREAM_TIMEOUT,
            )
            transformed = transform_response(response.content, public_origin(self), action)
            self.send_payload(response.status_code, transformed, response.headers.get("Content-Type", "application/soap+xml; charset=utf-8"))
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            log(f"{self.client_address[0]} {self.command} {self.path} action={action} upstream={response.status_code} elapsed_ms={elapsed_ms}")
        except Exception as exc:
            clear_synthetic_move()
            payload = f"upstream proxy error: {type(exc).__name__}: {exc}".encode("utf-8")
            self.send_payload(502, payload, "text/plain; charset=utf-8")
            log(f"ERROR {self.command} {self.path}: {type(exc).__name__}: {exc}")

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    log(
        f"starting listener={LISTEN_HOST}:{LISTEN_PORT} upstream={UPSTREAM_ORIGIN} "
        f"relative_move=continuous_pulse velocity={CONTINUOUS_VELOCITY} "
        f"pulse={PULSE_MIN_SECONDS}+magnitude*{PULSE_SECONDS_PER_FOV} max={PULSE_MAX_SECONDS}"
    )
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler).serve_forever()