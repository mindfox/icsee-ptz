from __future__ import annotations

import base64
import hashlib
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import requests

from .config import CameraConfig

SOAP = "http://www.w3.org/2003/05/soap-envelope"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BASE64_BINARY = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"
VELOCITY_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first(root: ET.Element | None, name: str) -> ET.Element | None:
    if root is None:
        return None
    return next((node for node in root.iter() if _local(node.tag) == name), None)


class TapoOnvifClient:
    """Native ONVIF client for Tapo PTZ, preset and home-position control."""

    def __init__(self, camera: CameraConfig):
        if not camera.username or not camera.password:
            raise ValueError(f"{camera.camera_id}: camera-account credentials are required")
        self.camera = camera
        self.username = camera.username
        self.password = camera.password
        self.port = int(camera.options.get("onvif_port", 2020))
        self.timeout = float(camera.options.get("timeout", 10))
        self.endpoint = str(camera.options.get("onvif_device_url", f"http://{camera.host}:{self.port}/onvif/service"))
        self.profile_token: str | None = None
        self.session = requests.Session()
        self.session.trust_env = False

    def _security(self) -> str:
        nonce = os.urandom(20)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = hashlib.sha1(nonce + created.encode() + self.password.encode()).digest()
        return (
            f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">'
            f"<wsse:UsernameToken><wsse:Username>{escape(self.username)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_DIGEST}">{base64.b64encode(digest).decode()}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{BASE64_BINARY}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
        )

    def _envelope(self, body: str, namespaces: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP}" {namespaces}><s:Header>{self._security()}</s:Header>'
            f"<s:Body>{body}</s:Body></s:Envelope>"
        ).encode()

    def _post(self, operation: str, namespace: str, body: str, namespaces: str) -> ET.Element:
        response = self.session.post(
            self.endpoint,
            data=self._envelope(body, namespaces),
            headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{namespace}/{operation}"',
                "Connection": "close",
            },
            timeout=self.timeout,
        )
        detail = response.text.strip().replace("\n", " ")[:1200]
        if response.status_code >= 400:
            raise RuntimeError(f"ONVIF HTTP {response.status_code}: {detail}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RuntimeError(f"ONVIF invalid XML: {detail}") from exc
        if _first(root, "Fault") is not None:
            raise RuntimeError(f"ONVIF SOAP fault: {detail}")
        return root

    def profile(self) -> str:
        if self.profile_token:
            return self.profile_token
        root = self._post("GetProfiles", TRT, "<trt:GetProfiles/>", f'xmlns:trt="{TRT}"')
        profiles = [node for node in root.iter() if _local(node.tag) == "Profiles"]
        selected = next((node for node in profiles if _first(node, "PTZConfiguration") is not None), profiles[0] if profiles else None)
        if selected is None or not selected.attrib.get("token"):
            raise RuntimeError("ONVIF GetProfiles returned no usable PTZ profile")
        self.profile_token = selected.attrib["token"]
        return self.profile_token

    def continuous_move(self, pan: float, tilt: float, seconds: float) -> None:
        token = self.profile()
        self._post(
            "ContinuousMove",
            TPTZ,
            f'<tptz:ContinuousMove><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken><tptz:Velocity>'
            f'<tt:PanTilt x="{pan:g}" y="{tilt:g}" space="{VELOCITY_SPACE}"/></tptz:Velocity></tptz:ContinuousMove>',
            f'xmlns:tptz="{TPTZ}" xmlns:tt="{TT}"',
        )
        time.sleep(max(0.02, seconds))
        self.stop()

    def stop(self) -> None:
        token = self.profile()
        self._post(
            "Stop",
            TPTZ,
            f"<tptz:Stop><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>false</tptz:Zoom></tptz:Stop>",
            f'xmlns:tptz="{TPTZ}"',
        )

    def get_status(self) -> dict[str, str | None]:
        token = self.profile()
        root = self._post(
            "GetStatus",
            TPTZ,
            f"<tptz:GetStatus><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken></tptz:GetStatus>",
            f'xmlns:tptz="{TPTZ}"',
        )
        pan_tilt = _first(_first(root, "Position"), "PanTilt")
        move_status = _first(_first(root, "MoveStatus"), "PanTilt")
        return {
            "profile_token": token,
            "pan": pan_tilt.attrib.get("x") if pan_tilt is not None else None,
            "tilt": pan_tilt.attrib.get("y") if pan_tilt is not None else None,
            "move_status": (move_status.text or "").strip() if move_status is not None else None,
        }

    def get_presets(self) -> list[dict]:
        token = self.profile()
        root = self._post(
            "GetPresets",
            TPTZ,
            f"<tptz:GetPresets><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken></tptz:GetPresets>",
            f'xmlns:tptz="{TPTZ}"',
        )
        presets = []
        for item in (node for node in root.iter() if _local(node.tag) == "Preset"):
            position = _first(item, "PTZPosition")
            pan_tilt = _first(position, "PanTilt")
            preset_token = item.attrib.get("token")
            if preset_token:
                presets.append({
                    "token": preset_token,
                    "name": ((_first(item, "Name").text or "").strip() if _first(item, "Name") is not None else ""),
                    "position": {
                        "pan_tilt": {
                            "x": pan_tilt.attrib.get("x") if pan_tilt is not None else None,
                            "y": pan_tilt.attrib.get("y") if pan_tilt is not None else None,
                            "space": pan_tilt.attrib.get("space") if pan_tilt is not None else None,
                        }
                    },
                })
        return presets

    def set_preset(self, preset_token: str, name: str) -> dict:
        profile = self.profile()
        name_xml = f"<tptz:PresetName>{escape(name)}</tptz:PresetName>" if name else ""
        token_xml = f"<tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken>" if preset_token else ""
        root = self._post(
            "SetPreset",
            TPTZ,
            f"<tptz:SetPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>{name_xml}{token_xml}</tptz:SetPreset>",
            f'xmlns:tptz="{TPTZ}"',
        )
        returned = _first(root, "PresetToken")
        return {"preset_token": (returned.text or "").strip() if returned is not None else preset_token, "name": name}

    def goto_preset(self, preset_token: str) -> dict:
        profile = self.profile()
        self._post(
            "GotoPreset",
            TPTZ,
            f"<tptz:GotoPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken><tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken></tptz:GotoPreset>",
            f'xmlns:tptz="{TPTZ}"',
        )
        return {"preset_token": preset_token}

    def remove_preset(self, preset_token: str) -> dict:
        profile = self.profile()
        self._post(
            "RemovePreset",
            TPTZ,
            f"<tptz:RemovePreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken><tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken></tptz:RemovePreset>",
            f'xmlns:tptz="{TPTZ}"',
        )
        return {"preset_token": preset_token}

    def goto_home(self) -> dict:
        profile = self.profile()
        self._post(
            "GotoHomePosition",
            TPTZ,
            f"<tptz:GotoHomePosition><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken></tptz:GotoHomePosition>",
            f'xmlns:tptz="{TPTZ}"',
        )
        return {"profile_token": profile}

    def set_home(self) -> dict:
        profile = self.profile()
        self._post(
            "SetHomePosition",
            TPTZ,
            f"<tptz:SetHomePosition><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken></tptz:SetHomePosition>",
            f'xmlns:tptz="{TPTZ}"',
        )
        return {"profile_token": profile}

    def diagnostics(self) -> dict:
        return {
            "collected_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "camera": {"id": self.camera.camera_id, "name": self.camera.name, "host": self.camera.host},
            "status": self.get_status(),
            "presets": self.get_presets(),
        }
