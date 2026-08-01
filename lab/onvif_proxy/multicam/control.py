from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import requests

from .config import CameraConfig

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TPTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
VELOCITY_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"
PRESET_SPEED_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"
ZOOM_VELOCITY_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_to_data(node: ET.Element | None):
    if node is None:
        return None
    children = list(node)
    result = {f"@{_local_name(key)}": value for key, value in node.attrib.items()}
    text = (node.text or "").strip()
    if not children:
        if result:
            if text:
                result["#text"] = text
            return result
        return text
    for child in children:
        key = _local_name(child.tag)
        value = element_to_data(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    if text:
        result["#text"] = text
    return result


class ProxyOnvifClient:
    """Manual test and diagnostics client routed through the per-camera ONVIF listener."""

    def __init__(self, camera: CameraConfig):
        self.camera = camera
        self.timeout = float(camera.options.get("timeout", 10))
        self.base = f"http://127.0.0.1:{camera.listen_port}"
        self.media_url = f"{self.base}/onvif/media_service"
        self.ptz_url = f"{self.base}/onvif/ptz_service"
        self.profile_token: str | None = None
        self.lock = threading.RLock()

    @staticmethod
    def _envelope(content: str, namespaces: str = "") -> str:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP_NS}" {namespaces}>'
            f'<s:Body>{content}</s:Body></s:Envelope>'
        )

    def _post(self, url: str, action: str, body: str) -> ET.Element:
        response = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                "Connection": "close",
            },
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:700]
            raise RuntimeError(f"ONVIF HTTP {response.status_code}: {detail}")
        if b"Fault" in response.content:
            detail = response.text.strip().replace("\n", " ")[:900]
            raise RuntimeError(f"ONVIF SOAP fault: {detail}")
        return ET.fromstring(response.content)

    def _query_ptz(self, action: str, xml: str) -> ET.Element:
        return self._post(
            self.ptz_url,
            f"{TPTZ_NS}/{action}",
            self._envelope(xml, f'xmlns:tptz="{TPTZ_NS}"'),
        )

    def get_profile_details(self) -> dict:
        root = self._post(
            self.media_url,
            f"{TRT_NS}/GetProfiles",
            self._envelope("<trt:GetProfiles/>", f'xmlns:trt="{TRT_NS}"'),
        )
        profiles = root.findall(f".//{{{TRT_NS}}}Profiles")
        if not profiles:
            raise RuntimeError("ONVIF GetProfiles returned no profiles")
        selected = profiles[0]
        token = selected.attrib.get("token")
        if not token:
            raise RuntimeError("ONVIF profile has no token")
        self.profile_token = token
        ptz_config = selected.find(f"{{{TT_NS}}}PTZConfiguration")
        node_token_node = ptz_config.find(f"{{{TT_NS}}}NodeToken") if ptz_config is not None else None
        return {
            "token": token,
            "name": (selected.findtext(f"{{{TT_NS}}}Name") or "").strip(),
            "ptz_configuration_token": ptz_config.attrib.get("token") if ptz_config is not None else None,
            "ptz_node_token": (node_token_node.text or "").strip() if node_token_node is not None else None,
            "raw": element_to_data(selected),
        }

    def _profile(self) -> str:
        if not self.profile_token:
            self.get_profile_details()
        if not self.profile_token:
            raise RuntimeError("ONVIF GetProfiles returned no usable profile")
        return self.profile_token

    def get_status(self) -> dict:
        token = self._profile()
        root = self._query_ptz(
            "GetStatus",
            f"<tptz:GetStatus><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken></tptz:GetStatus>",
        )
        status = root.find(f".//{{{TPTZ_NS}}}PTZStatus")
        return element_to_data(status) if status is not None else element_to_data(root)

    def diagnostics(self) -> dict:
        with self.lock:
            profile = self.get_profile_details()
            config_token = profile.get("ptz_configuration_token")
            node_token = profile.get("ptz_node_token")
            if not config_token:
                raise RuntimeError("selected media profile has no PTZ configuration token")

            config_root = self._query_ptz(
                "GetConfiguration",
                f"<tptz:GetConfiguration><tptz:PTZConfigurationToken>{escape(config_token)}</tptz:PTZConfigurationToken></tptz:GetConfiguration>",
            )
            config_node = config_root.find(f".//{{{TPTZ_NS}}}PTZConfiguration")
            configuration = element_to_data(config_node) if config_node is not None else element_to_data(config_root)

            if not node_token and config_node is not None:
                node_element = config_node.find(f"{{{TT_NS}}}NodeToken")
                node_token = (node_element.text or "").strip() if node_element is not None else None
            if not node_token:
                raise RuntimeError("PTZ configuration has no node token")

            node_root = self._query_ptz(
                "GetNode",
                f"<tptz:GetNode><tptz:NodeToken>{escape(node_token)}</tptz:NodeToken></tptz:GetNode>",
            )
            node_element = node_root.find(f".//{{{TPTZ_NS}}}PTZNode")
            node = element_to_data(node_element) if node_element is not None else element_to_data(node_root)

            options_root = self._query_ptz(
                "GetConfigurationOptions",
                f"<tptz:GetConfigurationOptions><tptz:ConfigurationToken>{escape(config_token)}</tptz:ConfigurationToken></tptz:GetConfigurationOptions>",
            )
            options_element = options_root.find(f".//{{{TPTZ_NS}}}PTZConfigurationOptions")
            options = element_to_data(options_element) if options_element is not None else element_to_data(options_root)

            return {
                "collected_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "camera": {
                    "id": self.camera.camera_id,
                    "name": self.camera.name,
                    "host": self.camera.host,
                    "listener": self.base,
                },
                "sources": {
                    "profile": "GetProfiles",
                    "configuration": "GetConfiguration",
                    "node": "GetNode",
                    "configuration_options": "GetConfigurationOptions",
                    "status": "GetStatus",
                },
                "profile": profile,
                "configuration": configuration,
                "node": node,
                "configuration_options": options,
                "status": self.get_status(),
            }

    def continuous_move(self, *, zoom: float, seconds: float) -> dict:
        with self.lock:
            token = self._profile()
            move = self._envelope(
                f'<tptz:ContinuousMove><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken>'
                f'<tptz:Velocity><tt:Zoom x="{zoom:g}" space="{ZOOM_VELOCITY_SPACE}"/>'
                f'</tptz:Velocity></tptz:ContinuousMove>',
                f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"',
            )
            self._post(self.ptz_url, f"{TPTZ_NS}/ContinuousMove", move)
            time.sleep(max(0.02, seconds))
            stop = self._envelope(
                f'<tptz:Stop><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken>'
                f'<tptz:PanTilt>false</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>',
                f'xmlns:tptz="{TPTZ_NS}"',
            )
            self._post(self.ptz_url, f"{TPTZ_NS}/Stop", stop)
            return {"profile_token": token, "zoom": zoom, "seconds": seconds}

    def get_presets(self) -> list[dict]:
        with self.lock:
            token = self._profile()
            root = self._post(
                self.ptz_url,
                f"{TPTZ_NS}/GetPresets",
                self._envelope(
                    f"<tptz:GetPresets><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken></tptz:GetPresets>",
                    f'xmlns:tptz="{TPTZ_NS}"',
                ),
            )
        result = []
        for item in root.findall(f".//{{{TPTZ_NS}}}Preset"):
            preset_token = item.attrib.get("token", "")
            name_node = item.find(f"{{{TT_NS}}}Name")
            position_node = item.find(f"{{{TT_NS}}}PTZPosition")
            pan_tilt = position_node.find(f"{{{TT_NS}}}PanTilt") if position_node is not None else None
            zoom = position_node.find(f"{{{TT_NS}}}Zoom") if position_node is not None else None
            if preset_token:
                result.append({
                    "token": preset_token,
                    "name": (name_node.text or "").strip() if name_node is not None else "",
                    "position": {
                        "pan_tilt": {
                            "x": pan_tilt.attrib.get("x") if pan_tilt is not None else None,
                            "y": pan_tilt.attrib.get("y") if pan_tilt is not None else None,
                            "space": pan_tilt.attrib.get("space") if pan_tilt is not None else None,
                        },
                        "zoom": {
                            "x": zoom.attrib.get("x") if zoom is not None else None,
                            "space": zoom.attrib.get("space") if zoom is not None else None,
                        },
                    },
                    "raw": element_to_data(item),
                })
        return result

    def set_preset(self, preset_token: str, name: str) -> dict:
        with self.lock:
            profile = self._profile()
            name_xml = f"<tptz:PresetName>{escape(name)}</tptz:PresetName>" if name else ""
            root = self._post(
                self.ptz_url,
                f"{TPTZ_NS}/SetPreset",
                self._envelope(
                    f"<tptz:SetPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
                    f"{name_xml}<tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken></tptz:SetPreset>",
                    f'xmlns:tptz="{TPTZ_NS}"',
                ),
            )
        token_node = root.find(f".//{{{TPTZ_NS}}}PresetToken")
        return {
            "requested_token": preset_token,
            "preset_token": token_node.text if token_node is not None else preset_token,
            "name": name,
        }

    def goto_preset(self, preset_token: str, speed_x: int = 1, speed_y: int = 1) -> dict:
        with self.lock:
            profile = self._profile()
            speed = (
                f'<tptz:Speed><tt:PanTilt x="{speed_x}" y="{speed_y}" '
                f'space="{PRESET_SPEED_SPACE}"/></tptz:Speed>'
            )
            self._post(
                self.ptz_url,
                f"{TPTZ_NS}/GotoPreset",
                self._envelope(
                    f"<tptz:GotoPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>"
                    f"<tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken>{speed}</tptz:GotoPreset>",
                    f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"',
                ),
            )
        return {"preset_token": preset_token, "speed_x": speed_x, "speed_y": speed_y}


class DvripSnapshotClient:
    def __init__(self, camera: CameraConfig):
        module_path = Path(str(camera.options.get("dvrip_module_path", "/opt/icsee_ptz/asyncio_dvrip.py")))
        spec = importlib.util.spec_from_file_location(
            f"icsee_asyncio_dvrip_{camera.camera_id}", module_path
        )
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load DVRIP module from {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.camera_type = module.DVRIPCam
        self.host = camera.host
        self.port = int(camera.options.get("dvrip_port", 34567))
        self.username = camera.username or ""
        self.password = camera.password or ""
        self.lock = threading.Lock()

    async def _snapshot(self) -> bytes:
        camera = self.camera_type(
            self.host,
            port=self.port,
            user=self.username,
            password=self.password,
        )
        try:
            if not await camera.login(asyncio.get_running_loop()):
                raise RuntimeError("camera login failed")
            result = await camera.snapshot(channel=0)
            if not result:
                raise RuntimeError("camera returned no snapshot")
            return bytes(result)
        finally:
            camera.close()

    def snapshot(self) -> bytes:
        with self.lock:
            return asyncio.run(self._snapshot())
