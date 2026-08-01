from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from .dashboard import load_index, load_script, load_stylesheet


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
                return self.send_payload(200, load_index(), "text/html; charset=utf-8")
            if path == "/assets/app.css":
                return self.send_payload(200, load_stylesheet(), "text/css; charset=utf-8")
            if path == "/assets/app.js":
                return self.send_payload(200, load_script(), "text/javascript; charset=utf-8")
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
            elif command == "diagnostics" and len(parts) == 4:
                result = self.server.runtime.diagnostics(camera_id)
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


def start_web(runtime):
    server = ThreadingHTTPServer((runtime.config.web_host, runtime.config.web_port), WebHandler)
    server.runtime = runtime
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
