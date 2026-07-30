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
SYNTHETIC_BASE_SECONDS = float(os.environ.get("SYNTHETIC_BASE_SECONDS", "0.20"))
SYNTHETIC_SECONDS_PER_FOV = float(os.environ.get("SYNTHETIC_SECONDS_PER_FOV", "2.0"))
SYNTHETIC_MIN_SECONDS = float(os.environ.get("SYNTHETIC_MIN_SECONDS", "0.15"))
SYNTHETIC_MAX_SECONDS = float(os.environ.get("SYNTHETIC_MAX_SECONDS", "3.0"))
FOV_TO_GENERIC_SCALE_X = float(os.environ.get("FOV_TO_GENERIC_SCALE_X", "1.0"))
FOV_TO_GENERIC_SCALE_Y = float(os.environ.get("FOV_TO_GENERIC_SCALE_Y", "1.0"))

UPSTREAM_ORIGIN = f"http://{CAMERA_HOST}:{CAMERA_ONVIF_PORT}"
AUTH = HTTPDigestAuth(CAMERA_USERNAME, CAMERA_PASSWORD)

_state_lock = threading.Lock()
_synthetic_moving_until = 0.0


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] [ONVIF-PROXY] {message}", flush=True)


def public_origin(handler: BaseHTTPRequestHandler) -> str:
    host = handler.headers.get("Host", "").strip()
    if not host:
        host = f"127.0.0.1:{LISTEN_PORT}"
    return f"http://{host}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find_all(root: ET.Element, name: str):
    return [node for node in root.iter() if local_name(node.tag) == name]


def parse_float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def mark_synthetic_move(x: float, y: float) -> float:
    magnitude = max(abs(x), abs(y))
    duration = SYNTHETIC_BASE_SECONDS + magnitude * SYNTHETIC_SECONDS_PER_FOV
    duration = max(SYNTHETIC_MIN_SECONDS, min(duration, SYNTHETIC_MAX_SECONDS))
    global _synthetic_moving_until
    with _state_lock:
        _synthetic_moving_until = max(_synthetic_moving_until, time.monotonic() + duration)
    return duration


def clear_synthetic_move() -> None:
    global _synthetic_moving_until
    with _state_lock:
        _synthetic_moving_until = 0.0


def synthetic_is_moving() -> bool:
    with _state_lock:
        return time.monotonic() < _synthetic_moving_until


def transform_request(body: bytes) -> tuple[bytes, dict]:
    metadata = {"action": "unknown", "fov_translation": None, "synthetic_seconds": None}
    if not body:
        return body, metadata
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return body, metadata

    body_nodes = find_all(root, "Body")
    if body_nodes and list(body_nodes[0]):
        metadata["action"] = local_name(list(body_nodes[0])[0].tag)

    if metadata["action"] == "RelativeMove":
        for pan_tilt in find_all(root, "PanTilt"):
            if pan_tilt.attrib.get("space") == FOV_SPACE:
                x = parse_float(pan_tilt.attrib.get("x"))
                y = parse_float(pan_tilt.attrib.get("y"))
                translated_x = max(-1.0, min(1.0, x * FOV_TO_GENERIC_SCALE_X))
                translated_y = max(-1.0, min(1.0, y * FOV_TO_GENERIC_SCALE_Y))
                pan_tilt.set("space", GENERIC_SPACE)
                pan_tilt.set("x", f"{translated_x:g}")
                pan_tilt.set("y", f"{translated_y:g}")
                metadata["fov_translation"] = {
                    "requested": {"x": x, "y": y, "space": FOV_SPACE},
                    "forwarded": {"x": translated_x, "y": translated_y, "space": GENERIC_SPACE},
                }
                metadata["synthetic_seconds"] = mark_synthetic_move(x, y)
                break
    elif metadata["action"] == "Stop":
        clear_synthetic_move()

    return ET.tostring(root, encoding="utf-8", xml_declaration=True), metadata


def ensure_fov_space(root: ET.Element) -> bool:
    changed = False
    for spaces in find_all(root, "Spaces") + find_all(root, "SupportedPTZSpaces"):
        existing = [node for node in list(spaces) if local_name(node.tag) == "RelativePanTiltTranslationSpace"]
        if any((next((u.text for u in node if local_name(u.tag) == "URI"), "") or "").strip() == FOV_SPACE for node in existing):
            continue
        template = existing[0] if existing else None
        entry = ET.Element(f"{{{TT}}}RelativePanTiltTranslationSpace")
        uri = ET.SubElement(entry, f"{{{TT}}}URI")
        uri.text = FOV_SPACE
        x_range = ET.SubElement(entry, f"{{{TT}}}XRange")
        ET.SubElement(x_range, f"{{{TT}}}Min").text = "-1"
        ET.SubElement(x_range, f"{{{TT}}}Max").text = "1"
        y_range = ET.SubElement(entry, f"{{{TT}}}YRange")
        ET.SubElement(y_range, f"{{{TT}}}Min").text = "-1"
        ET.SubElement(y_range, f"{{{TT}}}Max").text = "1"
        if template is not None:
            index = list(spaces).index(template) + 1
            spaces.insert(index, entry)
        else:
            spaces.append(entry)
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
    camera_patterns = (
        f"http://{CAMERA_HOST}:{CAMERA_ONVIF_PORT}",
        f"http://{CAMERA_HOST}",
    )
    for node in root.iter():
        if not node.text:
            continue
        text = node.text
        replaced = text
        for pattern in camera_patterns:
            replaced = replaced.replace(pattern, origin)
        if replaced != text:
            node.text = replaced
            changed = True
    return changed


def synthesize_status(root: ET.Element) -> bool:
    if not synthetic_is_moving():
        return False
    changed = False
    for move_status in find_all(root, "MoveStatus"):
        pan_tilt_nodes = [n for n in list(move_status) if local_name(n.tag) == "PanTilt"]
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
        text = re.sub(rf"http://{re.escape(CAMERA_HOST)}(?::{CAMERA_ONVIF_PORT})?", origin, text)
        return text.encode("utf-8")

    changed = rewrite_xaddrs(root, origin)
    if action in {"GetNode", "GetConfigurationOptions"}:
        changed = ensure_fov_space(root) or changed
    if action == "GetServiceCapabilities":
        changed = ensure_move_status_capability(root) or changed
    if action == "GetStatus":
        changed = synthesize_status(root) or changed
    return ET.tostring(root, encoding="utf-8", xml_declaration=True) if changed else body


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

    def forward(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        incoming = self.rfile.read(length) if length else b""
        outgoing, metadata = transform_request(incoming)
        upstream_url = f"{UPSTREAM_ORIGIN}{urlsplit(self.path).path}"
        if urlsplit(self.path).query:
            upstream_url += f"?{urlsplit(self.path).query}"
        headers = {}
        for name in ("Content-Type", "SOAPAction", "Accept", "User-Agent"):
            if name in self.headers:
                headers[name] = self.headers[name]
        headers["Connection"] = "close"

        started = time.monotonic()
        try:
            response = requests.request(
                self.command,
                upstream_url,
                data=outgoing if self.command != "GET" else None,
                headers=headers,
                auth=AUTH,
                timeout=UPSTREAM_TIMEOUT,
            )
            transformed = transform_response(response.content, public_origin(self), metadata["action"])
            self.send_response(response.status_code)
            content_type = response.headers.get("Content-Type", "application/soap+xml; charset=utf-8")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(transformed)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(transformed)
            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            detail = f" translation={metadata['fov_translation']} synthetic_seconds={metadata['synthetic_seconds']}" if metadata["fov_translation"] else ""
            log(f"{self.client_address[0]} {self.command} {self.path} action={metadata['action']} upstream={response.status_code} elapsed_ms={elapsed_ms}{detail}")
        except Exception as exc:
            clear_synthetic_move()
            payload = f"upstream proxy error: {type(exc).__name__}: {exc}".encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            log(f"ERROR {self.command} {self.path}: {type(exc).__name__}: {exc}")

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    log(f"starting listener={LISTEN_HOST}:{LISTEN_PORT} upstream={UPSTREAM_ORIGIN}")
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler).serve_forever()
