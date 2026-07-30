import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import requests
from requests.auth import HTTPDigestAuth

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TPTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
PRESET_SPEED_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"
GENERIC_TRANSLATION_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationGenericSpace"
FOV_TRANSLATION_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"
VELOCITY_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGenericSpace"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_to_data(node: ET.Element):
    children = list(node)
    result = {f"@{local_name(key)}": value for key, value in node.attrib.items()}
    text = (node.text or "").strip()
    if not children:
        if result:
            if text:
                result["#text"] = text
            return result
        return text
    for child in children:
        key = local_name(child.tag)
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


class OnvifClient:
    def __init__(self, host: str, port: int, username: str, password: str, timeout: float, log):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.log = log
        self.media_url = f"http://{host}:{port}/onvif/media_service"
        self.ptz_url = f"http://{host}:{port}/onvif/ptz_service"
        self.auth = HTTPDigestAuth(username, password)
        self.profile_token = None

    def envelope(self, content: str, namespaces: str = "") -> str:
        return f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="{SOAP_NS}" {namespaces}><s:Body>{content}</s:Body></s:Envelope>'''

    def soap_post(self, url: str, action: str, body: str) -> requests.Response:
        response = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"',
                "Connection": "close",
            },
            auth=self.auth,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            detail = response.text.strip().replace("\n", " ")[:700]
            raise RuntimeError(f"ONVIF HTTP {response.status_code}: {detail}")
        if b"Fault" in response.content:
            detail = response.text.strip().replace("\n", " ")[:900]
            raise RuntimeError(f"ONVIF SOAP fault: {detail}")
        return response

    def query_ptz(self, action: str, xml: str) -> ET.Element:
        body = self.envelope(xml, f'xmlns:tptz="{TPTZ_NS}"')
        return ET.fromstring(self.soap_post(self.ptz_url, f"{TPTZ_NS}/{action}", body).content)

    def get_profile_details(self) -> dict:
        root = ET.fromstring(
            self.soap_post(
                self.media_url,
                f"{TRT_NS}/GetProfiles",
                self.envelope("<trt:GetProfiles/>", f'xmlns:trt="{TRT_NS}"'),
            ).content
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

    def get_profile_token(self) -> str:
        if not self.profile_token:
            details = self.get_profile_details()
            self.log("INFO", f"ONVIF profile selected: {details['token']}")
        return self.profile_token

    def get_status(self) -> dict:
        token = self.get_profile_token()
        root = self.query_ptz("GetStatus", f"<tptz:GetStatus><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken></tptz:GetStatus>")
        status_el = root.find(f".//{{{TPTZ_NS}}}PTZStatus")
        return element_to_data(status_el) if status_el is not None else element_to_data(root)

    def stop(self) -> dict:
        token = self.get_profile_token()
        body = self.envelope(
            f"<tptz:Stop><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>",
            f'xmlns:tptz="{TPTZ_NS}"',
        )
        response = self.soap_post(self.ptz_url, f"{TPTZ_NS}/Stop", body)
        return {"status": response.status_code, "profile_token": token}

    def relative_move(self, x: float, y: float, space: str, speed: float = 1.0) -> dict:
        token = self.get_profile_token()
        body = self.envelope(
            f'<tptz:RelativeMove><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken>'
            f'<tptz:Translation><tt:PanTilt x="{x:g}" y="{y:g}" space="{escape(space)}"/></tptz:Translation>'
            f'<tptz:Speed><tt:PanTilt x="{speed:g}" y="{speed:g}" space="{PRESET_SPEED_SPACE}"/></tptz:Speed>'
            f'</tptz:RelativeMove>',
            f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"',
        )
        started = time.monotonic()
        response = self.soap_post(self.ptz_url, f"{TPTZ_NS}/RelativeMove", body)
        return {"status": response.status_code, "profile_token": token, "translation": {"x": x, "y": y, "space": space}, "speed": speed, "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}

    def begin_continuous_move(self, x: float, y: float) -> dict:
        token = self.get_profile_token()
        body = self.envelope(
            f'<tptz:ContinuousMove><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken>'
            f'<tptz:Velocity><tt:PanTilt x="{x:g}" y="{y:g}" space="{VELOCITY_SPACE}"/></tptz:Velocity>'
            f'</tptz:ContinuousMove>',
            f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"',
        )
        response = self.soap_post(self.ptz_url, f"{TPTZ_NS}/ContinuousMove", body)
        return {"status": response.status_code, "profile_token": token, "velocity": {"x": x, "y": y, "space": VELOCITY_SPACE}}

    def diagnostics(self) -> dict:
        profile = self.get_profile_details()
        config_token = profile.get("ptz_configuration_token")
        node_token = profile.get("ptz_node_token")
        if not config_token:
            raise RuntimeError("selected media profile has no PTZ configuration token")
        cfg_root = self.query_ptz("GetConfiguration", f"<tptz:GetConfiguration><tptz:PTZConfigurationToken>{escape(config_token)}</tptz:PTZConfigurationToken></tptz:GetConfiguration>")
        cfg_node = cfg_root.find(f".//{{{TPTZ_NS}}}PTZConfiguration")
        configuration = element_to_data(cfg_node) if cfg_node is not None else element_to_data(cfg_root)
        if not node_token and cfg_node is not None:
            node_el = cfg_node.find(f"{{{TT_NS}}}NodeToken")
            node_token = (node_el.text or "").strip() if node_el is not None else None
        if not node_token:
            raise RuntimeError("PTZ configuration has no node token")
        node_root = self.query_ptz("GetNode", f"<tptz:GetNode><tptz:NodeToken>{escape(node_token)}</tptz:NodeToken></tptz:GetNode>")
        node_el = node_root.find(f".//{{{TPTZ_NS}}}PTZNode")
        node = element_to_data(node_el) if node_el is not None else element_to_data(node_root)
        options_root = self.query_ptz("GetConfigurationOptions", f"<tptz:GetConfigurationOptions><tptz:ConfigurationToken>{escape(config_token)}</tptz:ConfigurationToken></tptz:GetConfigurationOptions>")
        options_el = options_root.find(f".//{{{TPTZ_NS}}}PTZConfigurationOptions")
        options = element_to_data(options_el) if options_el is not None else element_to_data(options_root)
        return {
            "collected_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "sources": {"profile": "GetProfiles", "configuration": "GetConfiguration", "node": "GetNode", "configuration_options": "GetConfigurationOptions", "status": "GetStatus"},
            "profile": profile,
            "configuration": configuration,
            "node": node,
            "configuration_options": options,
            "status": self.get_status(),
        }

    def continuous_move(self, command: str, pulse_seconds: float, velocity: tuple[float, float, float]) -> dict:
        x, y, zoom = velocity
        token = self.get_profile_token()
        velocity_xml = f'<tt:Zoom x="{zoom:g}" space="http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGenericSpace"/>' if command.startswith("zoom_") else f'<tt:PanTilt x="{x:g}" y="{y:g}" space="{VELOCITY_SPACE}"/>'
        move_body = self.envelope(f"<tptz:ContinuousMove><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken><tptz:Velocity>{velocity_xml}</tptz:Velocity></tptz:ContinuousMove>", f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"')
        started = self.soap_post(self.ptz_url, f"{TPTZ_NS}/ContinuousMove", move_body)
        time.sleep(pulse_seconds)
        stopped = self.stop()
        return {"start_status": started.status_code, "stop_status": stopped["status"], "profile_token": token, "velocity": {"x": x, "y": y, "zoom": zoom}, "pulse_seconds": pulse_seconds}

    def get_presets(self) -> list[dict]:
        token = self.get_profile_token()
        root = self.query_ptz("GetPresets", f"<tptz:GetPresets><tptz:ProfileToken>{escape(token)}</tptz:ProfileToken></tptz:GetPresets>")
        presets = []
        for item in root.findall(f".//{{{TPTZ_NS}}}Preset"):
            preset_token = item.attrib.get("token", "")
            name_node = item.find(f"{{{TT_NS}}}Name")
            preset_name = name_node.text.strip() if name_node is not None and name_node.text else ""
            position_node = item.find(f"{{{TT_NS}}}PTZPosition")
            pan_tilt = position_node.find(f"{{{TT_NS}}}PanTilt") if position_node is not None else None
            zoom = position_node.find(f"{{{TT_NS}}}Zoom") if position_node is not None else None
            if preset_token:
                presets.append({
                    "token": preset_token,
                    "name": preset_name,
                    "position": {
                        "pan_tilt": {"x": pan_tilt.attrib.get("x") if pan_tilt is not None else None, "y": pan_tilt.attrib.get("y") if pan_tilt is not None else None, "space": pan_tilt.attrib.get("space") if pan_tilt is not None else None},
                        "zoom": {"x": zoom.attrib.get("x") if zoom is not None else None, "space": zoom.attrib.get("space") if zoom is not None else None},
                    },
                    "raw": element_to_data(item),
                })
        return presets

    def set_preset(self, name: str, preset_token: str) -> dict:
        profile = self.get_profile_token()
        name_xml = f"<tptz:PresetName>{escape(name)}</tptz:PresetName>" if name else ""
        body = self.envelope(f"<tptz:SetPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken>{name_xml}<tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken></tptz:SetPreset>", f'xmlns:tptz="{TPTZ_NS}"')
        response = self.soap_post(self.ptz_url, f"{TPTZ_NS}/SetPreset", body)
        root = ET.fromstring(response.content)
        token_node = root.find(f".//{{{TPTZ_NS}}}PresetToken")
        return {"status": response.status_code, "requested_token": preset_token, "preset_token": token_node.text if token_node is not None else None, "name": name}

    def goto_preset(self, preset_token: str, speed_x: int, speed_y: int) -> dict:
        profile = self.get_profile_token()
        speed_xml = f'<tptz:Speed><tt:PanTilt x="{speed_x}" y="{speed_y}" space="{PRESET_SPEED_SPACE}"/></tptz:Speed>'
        body = self.envelope(f"<tptz:GotoPreset><tptz:ProfileToken>{escape(profile)}</tptz:ProfileToken><tptz:PresetToken>{escape(preset_token)}</tptz:PresetToken>{speed_xml}</tptz:GotoPreset>", f'xmlns:tptz="{TPTZ_NS}" xmlns:tt="{TT_NS}"')
        started = time.monotonic()
        response = self.soap_post(self.ptz_url, f"{TPTZ_NS}/GotoPreset", body)
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        return {"status": response.status_code, "preset_token": preset_token, "speed": {"x": speed_x, "y": speed_y, "space": PRESET_SPEED_SPACE}, "elapsed_ms": elapsed_ms}
