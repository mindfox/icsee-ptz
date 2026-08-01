from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import requests

from .config import CameraConfig, load_config

SOAP_NS = "http://www.w3.org/2003/05/soap-envelope"
TDS_NS = "http://www.onvif.org/ver10/device/wsdl"
TRT_NS = "http://www.onvif.org/ver10/media/wsdl"
TPTZ_NS = "http://www.onvif.org/ver20/ptz/wsdl"
TT_NS = "http://www.onvif.org/ver10/schema"
WSSE_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
BASE64_BINARY = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_to_data(node: ET.Element | None) -> Any:
    if node is None:
        return None
    children = list(node)
    result: dict[str, Any] = {
        f"@{_local_name(key)}": value for key, value in node.attrib.items()
    }
    text = (node.text or "").strip()
    if not children:
        if result:
            if text:
                result["#text"] = text
            return result
        return text
    for child in children:
        key = _local_name(child.tag)
        value = _element_to_data(child)
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    if text:
        result["#text"] = text
    return result


def _first_text(root: ET.Element, local_name: str) -> str | None:
    for node in root.iter():
        if _local_name(node.tag) == local_name and node.text:
            text = node.text.strip()
            if text:
                return text
    return None


def _find_all(root: ET.Element, local_name: str) -> list[ET.Element]:
    return [node for node in root.iter() if _local_name(node.tag) == local_name]


@dataclass(frozen=True)
class ProbeResponse:
    operation: str
    endpoint: str
    status_code: int
    root: ET.Element


class TapoOnvifProbe:
    """Read-only native ONVIF probe for Tapo cameras.

    This class intentionally implements no movement methods. It only queries
    camera-reported device, media, and PTZ metadata so the driver can later be
    implemented from observed capabilities instead of assumptions.
    """

    def __init__(self, camera: CameraConfig):
        if not camera.username or not camera.password:
            raise ValueError(
                f"{camera.camera_id}: camera-account username and password are required"
            )
        self.camera = camera
        self.username = camera.username
        self.password = camera.password
        self.timeout = float(camera.options.get("timeout", 10))
        self.port = int(camera.options.get("onvif_port", 2020))
        self.device_url = str(
            camera.options.get(
                "onvif_device_url",
                f"http://{camera.host}:{self.port}/onvif/service",
            )
        )
        self.created_at: datetime | None = None

    def _security_header(self) -> str:
        raw_nonce = os.urandom(20)
        created_dt = self.created_at or datetime.now(timezone.utc)
        created = created_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = hashlib.sha1(
            raw_nonce + created.encode("utf-8") + self.password.encode("utf-8")
        ).digest()
        return (
            f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE_NS}" '
            f'xmlns:wsu="{WSU_NS}">'
            f"<wsse:UsernameToken>"
            f"<wsse:Username>{escape(self.username)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_DIGEST}">'
            f"{base64.b64encode(digest).decode('ascii')}"
            f"</wsse:Password>"
            f'<wsse:Nonce EncodingType="{BASE64_BINARY}">'
            f"{base64.b64encode(raw_nonce).decode('ascii')}"
            f"</wsse:Nonce>"
            f'<wsu:Created>{created}</wsu:Created>'
            f"</wsse:UsernameToken>"
            f"</wsse:Security>"
        )

    def _envelope(self, content: str, *, authenticated: bool, namespaces: str = "") -> str:
        header = self._security_header() if authenticated else ""
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP_NS}" {namespaces}>'
            f"<s:Header>{header}</s:Header>"
            f"<s:Body>{content}</s:Body>"
            f"</s:Envelope>"
        )

    def _post(
        self,
        endpoint: str,
        operation: str,
        namespace: str,
        content: str,
        *,
        authenticated: bool = True,
        namespaces: str = "",
    ) -> ProbeResponse:
        body = self._envelope(
            content,
            authenticated=authenticated,
            namespaces=namespaces,
        )
        response = requests.post(
            endpoint,
            data=body.encode("utf-8"),
            headers={
                "Content-Type": (
                    "application/soap+xml; charset=utf-8; "
                    f'action="{namespace}/{operation}"'
                ),
                "User-Agent": "icsee-ptz-tapo-probe",
                "Accept": "*/*",
                "Connection": "close",
            },
            timeout=self.timeout,
        )
        detail = response.text.strip().replace("\n", " ")[:1200]
        if response.status_code >= 400:
            raise RuntimeError(
                f"{operation} failed at {endpoint}: HTTP {response.status_code}: {detail}"
            )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"{operation} returned invalid XML from {endpoint}: {detail}"
            ) from exc
        if any(_local_name(node.tag) == "Fault" for node in root.iter()):
            raise RuntimeError(f"{operation} returned SOAP Fault from {endpoint}: {detail}")
        return ProbeResponse(operation, endpoint, response.status_code, root)

    def _camera_time(self) -> dict[str, Any]:
        response = self._post(
            self.device_url,
            "GetSystemDateAndTime",
            TDS_NS,
            "<tds:GetSystemDateAndTime/>",
            authenticated=False,
            namespaces=f'xmlns:tds="{TDS_NS}"',
        )
        utc = next(
            (
                node
                for node in response.root.iter()
                if _local_name(node.tag) == "UTCDateTime"
            ),
            None,
        )
        if utc is not None:
            fields = {
                _local_name(node.tag): (node.text or "").strip()
                for node in utc.iter()
                if node.text and _local_name(node.tag)
                in {"Year", "Month", "Day", "Hour", "Minute", "Second"}
            }
            try:
                self.created_at = datetime(
                    int(fields["Year"]),
                    int(fields["Month"]),
                    int(fields["Day"]),
                    int(fields["Hour"]),
                    int(fields["Minute"]),
                    int(fields["Second"]),
                    tzinfo=timezone.utc,
                )
            except (KeyError, TypeError, ValueError):
                self.created_at = None
        return _element_to_data(response.root)

    @staticmethod
    def _service_urls(root: ET.Element) -> dict[str, str]:
        urls: dict[str, str] = {}
        for service in _find_all(root, "Service"):
            namespace = _first_text(service, "Namespace")
            xaddr = _first_text(service, "XAddr")
            if namespace and xaddr:
                urls[namespace] = xaddr
        return urls

    def run(self) -> dict[str, Any]:
        results: dict[str, Any] = {
            "collected_at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
            "camera": {
                "id": self.camera.camera_id,
                "name": self.camera.name,
                "host": self.camera.host,
                "onvif_port": self.port,
                "device_url": self.device_url,
            },
            "operations": {},
        }

        results["operations"]["GetSystemDateAndTime"] = self._camera_time()

        services = self._post(
            self.device_url,
            "GetServices",
            TDS_NS,
            "<tds:GetServices><tds:IncludeCapability>true</tds:IncludeCapability></tds:GetServices>",
            namespaces=f'xmlns:tds="{TDS_NS}"',
        )
        results["operations"]["GetServices"] = _element_to_data(services.root)
        service_urls = self._service_urls(services.root)
        results["service_urls"] = service_urls

        capabilities = self._post(
            self.device_url,
            "GetCapabilities",
            TDS_NS,
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
            namespaces=f'xmlns:tds="{TDS_NS}"',
        )
        results["operations"]["GetCapabilities"] = _element_to_data(capabilities.root)

        device_info = self._post(
            self.device_url,
            "GetDeviceInformation",
            TDS_NS,
            "<tds:GetDeviceInformation/>",
            namespaces=f'xmlns:tds="{TDS_NS}"',
        )
        results["operations"]["GetDeviceInformation"] = _element_to_data(
            device_info.root
        )

        media_url = service_urls.get(TRT_NS)
        ptz_url = service_urls.get(TPTZ_NS)
        if not media_url:
            media_url = f"http://{self.camera.host}:{self.port}/onvif/service"
        if not ptz_url:
            ptz_url = f"http://{self.camera.host}:{self.port}/onvif/service"
        results["resolved_endpoints"] = {"media": media_url, "ptz": ptz_url}

        profiles = self._post(
            media_url,
            "GetProfiles",
            TRT_NS,
            "<trt:GetProfiles/>",
            namespaces=f'xmlns:trt="{TRT_NS}"',
        )
        results["operations"]["GetProfiles"] = _element_to_data(profiles.root)

        profile_nodes = _find_all(profiles.root, "Profiles")
        selected_profile: ET.Element | None = None
        for profile in profile_nodes:
            if any(_local_name(node.tag) == "PTZConfiguration" for node in profile.iter()):
                selected_profile = profile
                break
        if selected_profile is None and profile_nodes:
            selected_profile = profile_nodes[0]
        if selected_profile is None:
            raise RuntimeError("GetProfiles returned no media profiles")

        profile_token = selected_profile.attrib.get("token")
        ptz_config = next(
            (
                node
                for node in selected_profile.iter()
                if _local_name(node.tag) == "PTZConfiguration"
            ),
            None,
        )
        config_token = ptz_config.attrib.get("token") if ptz_config is not None else None
        node_token = _first_text(ptz_config, "NodeToken") if ptz_config is not None else None
        results["selected"] = {
            "profile_token": profile_token,
            "ptz_configuration_token": config_token,
            "ptz_node_token": node_token,
        }

        nodes = self._post(
            ptz_url,
            "GetNodes",
            TPTZ_NS,
            "<tptz:GetNodes/>",
            namespaces=f'xmlns:tptz="{TPTZ_NS}"',
        )
        results["operations"]["GetNodes"] = _element_to_data(nodes.root)

        configurations = self._post(
            ptz_url,
            "GetConfigurations",
            TPTZ_NS,
            "<tptz:GetConfigurations/>",
            namespaces=f'xmlns:tptz="{TPTZ_NS}"',
        )
        results["operations"]["GetConfigurations"] = _element_to_data(
            configurations.root
        )

        if config_token:
            configuration = self._post(
                ptz_url,
                "GetConfiguration",
                TPTZ_NS,
                "<tptz:GetConfiguration>"
                f"<tptz:PTZConfigurationToken>{escape(config_token)}</tptz:PTZConfigurationToken>"
                "</tptz:GetConfiguration>",
                namespaces=f'xmlns:tptz="{TPTZ_NS}"',
            )
            results["operations"]["GetConfiguration"] = _element_to_data(
                configuration.root
            )

            options = self._post(
                ptz_url,
                "GetConfigurationOptions",
                TPTZ_NS,
                "<tptz:GetConfigurationOptions>"
                f"<tptz:ConfigurationToken>{escape(config_token)}</tptz:ConfigurationToken>"
                "</tptz:GetConfigurationOptions>",
                namespaces=f'xmlns:tptz="{TPTZ_NS}"',
            )
            results["operations"]["GetConfigurationOptions"] = _element_to_data(
                options.root
            )

        if node_token:
            node = self._post(
                ptz_url,
                "GetNode",
                TPTZ_NS,
                "<tptz:GetNode>"
                f"<tptz:NodeToken>{escape(node_token)}</tptz:NodeToken>"
                "</tptz:GetNode>",
                namespaces=f'xmlns:tptz="{TPTZ_NS}"',
            )
            results["operations"]["GetNode"] = _element_to_data(node.root)

        if profile_token:
            status = self._post(
                ptz_url,
                "GetStatus",
                TPTZ_NS,
                "<tptz:GetStatus>"
                f"<tptz:ProfileToken>{escape(profile_token)}</tptz:ProfileToken>"
                "</tptz:GetStatus>",
                namespaces=f'xmlns:tptz="{TPTZ_NS}"',
            )
            results["operations"]["GetStatus"] = _element_to_data(status.root)

            presets = self._post(
                ptz_url,
                "GetPresets",
                TPTZ_NS,
                "<tptz:GetPresets>"
                f"<tptz:ProfileToken>{escape(profile_token)}</tptz:ProfileToken>"
                "</tptz:GetPresets>",
                namespaces=f'xmlns:tptz="{TPTZ_NS}"',
            )
            results["operations"]["GetPresets"] = _element_to_data(presets.root)

        return results


def _select_camera(config_path: str, camera_id: str) -> CameraConfig:
    config = load_config(config_path)
    for camera in config.cameras:
        if camera.camera_id == camera_id:
            if camera.driver != "tapo_c200":
                raise ValueError(f"{camera_id}: configured driver is {camera.driver}, not tapo_c200")
            return camera
    raise ValueError(f"unknown camera id: {camera_id}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a read-only native ONVIF probe against a configured Tapo camera"
    )
    parser.add_argument("camera_id", help="camera id from cameras.yaml")
    parser.add_argument("--config", default="/config/cameras.yaml")
    parser.add_argument("--output", help="optional JSON output file")
    args = parser.parse_args()

    try:
        camera = _select_camera(args.config, args.camera_id)
        result = TapoOnvifProbe(camera).run()
        rendered = json.dumps(result, indent=2, sort_keys=False)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(rendered)
                handle.write("\n")
        print(rendered)
    except Exception as exc:
        print(f"Tapo ONVIF probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
