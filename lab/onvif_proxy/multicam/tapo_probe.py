from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from xml.sax.saxutils import escape

import requests

from .config import CameraConfig, load_config

SOAP = "http://www.w3.org/2003/05/soap-envelope"
TDS = "http://www.onvif.org/ver10/device/wsdl"
TRT = "http://www.onvif.org/ver10/media/wsdl"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PASSWORD_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
BASE64_BINARY = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def data(node: ET.Element | None) -> Any:
    if node is None:
        return None
    children = list(node)
    out: dict[str, Any] = {f"@{local(k)}": v for k, v in node.attrib.items()}
    text = (node.text or "").strip()
    if not children:
        if out:
            if text:
                out["#text"] = text
            return out
        return text
    for child in children:
        key, value = local(child.tag), data(child)
        if key in out:
            if not isinstance(out[key], list):
                out[key] = [out[key]]
            out[key].append(value)
        else:
            out[key] = value
    if text:
        out["#text"] = text
    return out


def first(root: ET.Element | None, name: str) -> ET.Element | None:
    if root is None:
        return None
    return next((n for n in root.iter() if local(n.tag) == name), None)


def text(root: ET.Element | None, name: str) -> str | None:
    node = first(root, name)
    value = (node.text or "").strip() if node is not None else ""
    return value or None


class TapoOnvifProbe:
    """Read-only native ONVIF probe. No movement operations are implemented."""

    def __init__(self, camera: CameraConfig):
        if not camera.username or not camera.password:
            raise ValueError(f"{camera.camera_id}: camera-account credentials are required")
        self.camera = camera
        self.username = camera.username
        self.password = camera.password
        self.port = int(camera.options.get("onvif_port", 2020))
        self.timeout = float(camera.options.get("timeout", 10))
        configured = camera.options.get("onvif_device_url")
        self.device_candidates = [
            str(configured) if configured else "",
            f"http://{camera.host}:{self.port}/onvif/service",
            f"http://{camera.host}:{self.port}/onvif/device_service",
        ]
        self.device_candidates = list(dict.fromkeys(x for x in self.device_candidates if x))
        self.device_url = self.device_candidates[0]
        self.camera_time: datetime | None = None
        self.session = requests.Session()
        self.session.trust_env = False

    def security(self) -> str:
        nonce = os.urandom(20)
        created_dt = self.camera_time or datetime.now(timezone.utc)
        created = created_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = hashlib.sha1(
            nonce + created.encode() + self.password.encode()
        ).digest()
        return (
            f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">'
            f"<wsse:UsernameToken>"
            f"<wsse:Username>{escape(self.username)}</wsse:Username>"
            f'<wsse:Password Type="{PASSWORD_DIGEST}">{base64.b64encode(digest).decode()}</wsse:Password>'
            f'<wsse:Nonce EncodingType="{BASE64_BINARY}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
            f"<wsu:Created>{created}</wsu:Created>"
            f"</wsse:UsernameToken></wsse:Security>"
        )

    def envelope(self, body: str, authenticated: bool, namespaces: str) -> bytes:
        header = self.security() if authenticated else ""
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP}" {namespaces}>'
            f"<s:Header>{header}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
        )
        return xml.encode()

    def post(
        self,
        endpoint: str,
        operation: str,
        namespace: str,
        body: str,
        *,
        authenticated: bool = True,
        namespaces: str,
    ) -> ET.Element:
        response = self.session.post(
            endpoint,
            data=self.envelope(body, authenticated, namespaces),
            headers={
                "Content-Type": f'application/soap+xml; charset=utf-8; action="{namespace}/{operation}"',
                "User-Agent": "icsee-ptz-tapo-probe",
                "Accept": "*/*",
                "Connection": "close",
            },
            timeout=self.timeout,
        )
        detail = response.text.strip().replace("\n", " ")[:1200]
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} from {endpoint}: {detail}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RuntimeError(f"invalid XML from {endpoint}: {detail}") from exc
        if first(root, "Fault") is not None:
            raise RuntimeError(f"SOAP Fault from {endpoint}: {detail}")
        return root

    def call(
        self,
        results: dict[str, Any],
        name: str,
        endpoint: str,
        namespace: str,
        body: str,
        *,
        authenticated: bool = True,
        namespaces: str,
    ) -> ET.Element | None:
        try:
            root = self.post(
                endpoint,
                name,
                namespace,
                body,
                authenticated=authenticated,
                namespaces=namespaces,
            )
            results["operations"][name] = {
                "ok": True,
                "endpoint": endpoint,
                "response": data(root),
            }
            return root
        except Exception as exc:
            results["operations"][name] = {
                "ok": False,
                "endpoint": endpoint,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return None

    def establish_device_endpoint(self, results: dict[str, Any]) -> ET.Element:
        errors = []
        for endpoint in self.device_candidates:
            try:
                root = self.post(
                    endpoint,
                    "GetSystemDateAndTime",
                    TDS,
                    "<tds:GetSystemDateAndTime/>",
                    authenticated=False,
                    namespaces=f'xmlns:tds="{TDS}"',
                )
                self.device_url = endpoint
                results["operations"]["GetSystemDateAndTime"] = {
                    "ok": True,
                    "endpoint": endpoint,
                    "response": data(root),
                }
                return root
            except Exception as exc:
                errors.append(f"{endpoint}: {type(exc).__name__}: {exc}")
        raise RuntimeError("; ".join(errors))

    def set_camera_time(self, root: ET.Element) -> None:
        utc = first(root, "UTCDateTime")
        fields = {name: text(utc, name) for name in ("Year", "Month", "Day", "Hour", "Minute", "Second")}
        try:
            self.camera_time = datetime(
                int(fields["Year"]), int(fields["Month"]), int(fields["Day"]),
                int(fields["Hour"]), int(fields["Minute"]), int(fields["Second"]),
                tzinfo=timezone.utc,
            )
        except (TypeError, ValueError):
            self.camera_time = None

    @staticmethod
    def service_urls(root: ET.Element | None) -> dict[str, str]:
        urls: dict[str, str] = {}
        if root is None:
            return urls
        for service in (n for n in root.iter() if local(n.tag) == "Service"):
            namespace, xaddr = text(service, "Namespace"), text(service, "XAddr")
            if namespace and xaddr:
                urls[namespace] = xaddr
        return urls

    def run(self) -> dict[str, Any]:
        results: dict[str, Any] = {
            "collected_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "camera": {
                "id": self.camera.camera_id,
                "name": self.camera.name,
                "host": self.camera.host,
                "onvif_port": self.port,
            },
            "operations": {},
        }

        clock = self.establish_device_endpoint(results)
        self.set_camera_time(clock)
        results["camera"]["device_url"] = self.device_url

        services = self.call(
            results, "GetServices", self.device_url, TDS,
            "<tds:GetServices><tds:IncludeCapability>true</tds:IncludeCapability></tds:GetServices>",
            namespaces=f'xmlns:tds="{TDS}"',
        )
        self.call(
            results, "GetCapabilities", self.device_url, TDS,
            "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>",
            namespaces=f'xmlns:tds="{TDS}"',
        )
        self.call(
            results, "GetDeviceInformation", self.device_url, TDS,
            "<tds:GetDeviceInformation/>", namespaces=f'xmlns:tds="{TDS}"',
        )

        urls = self.service_urls(services)
        results["service_urls"] = urls
        media_url = urls.get(TRT, self.device_url)
        ptz_url = urls.get(TPTZ, self.device_url)
        results["resolved_endpoints"] = {"media": media_url, "ptz": ptz_url}

        profiles = self.call(
            results, "GetProfiles", media_url, TRT,
            "<trt:GetProfiles/>", namespaces=f'xmlns:trt="{TRT}"',
        )
        profile_nodes = [n for n in profiles.iter() if local(n.tag) == "Profiles"] if profiles is not None else []
        selected = next(
            (p for p in profile_nodes if first(p, "PTZConfiguration") is not None),
            profile_nodes[0] if profile_nodes else None,
        )
        ptz_config = first(selected, "PTZConfiguration")
        profile_token = selected.attrib.get("token") if selected is not None else None
        config_token = ptz_config.attrib.get("token") if ptz_config is not None else None
        node_token = text(ptz_config, "NodeToken")
        results["selected"] = {
            "profile_token": profile_token,
            "ptz_configuration_token": config_token,
            "ptz_node_token": node_token,
        }

        self.call(results, "GetNodes", ptz_url, TPTZ, "<tptz:GetNodes/>", namespaces=f'xmlns:tptz="{TPTZ}"')
        self.call(results, "GetConfigurations", ptz_url, TPTZ, "<tptz:GetConfigurations/>", namespaces=f'xmlns:tptz="{TPTZ}"')

        if config_token:
            self.call(
                results, "GetConfiguration", ptz_url, TPTZ,
                f"<tptz:GetConfiguration><tptz:PTZConfigurationToken>{escape(config_token)}</tptz:PTZConfigurationToken></tptz:GetConfiguration>",
                namespaces=f'xmlns:tptz="{TPTZ}"',
            )
            self.call(
                results, "GetConfigurationOptions", ptz_url, TPTZ,
                f"<tptz:GetConfigurationOptions><tptz:ConfigurationToken>{escape(config_token)}</tptz:ConfigurationToken></tptz:GetConfigurationOptions>",
                namespaces=f'xmlns:tptz="{TPTZ}"',
            )
        if node_token:
            self.call(
                results, "GetNode", ptz_url, TPTZ,
                f"<tptz:GetNode><tptz:NodeToken>{escape(node_token)}</tptz:NodeToken></tptz:GetNode>",
                namespaces=f'xmlns:tptz="{TPTZ}"',
            )
        if profile_token:
            self.call(
                results, "GetStatus", ptz_url, TPTZ,
                f"<tptz:GetStatus><tptz:ProfileToken>{escape(profile_token)}</tptz:ProfileToken></tptz:GetStatus>",
                namespaces=f'xmlns:tptz="{TPTZ}"',
            )
            self.call(
                results, "GetPresets", ptz_url, TPTZ,
                f"<tptz:GetPresets><tptz:ProfileToken>{escape(profile_token)}</tptz:ProfileToken></tptz:GetPresets>",
                namespaces=f'xmlns:tptz="{TPTZ}"',
            )
        return results


def configured_camera(path: str, camera_id: str) -> CameraConfig:
    for camera in load_config(path).cameras:
        if camera.camera_id == camera_id:
            if camera.driver != "tapo_c200":
                raise ValueError(f"{camera_id}: driver is {camera.driver}, not tapo_c200")
            return camera
    raise ValueError(f"unknown camera id: {camera_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only native ONVIF probe for a configured Tapo camera")
    parser.add_argument("camera_id")
    parser.add_argument("--config", default="/config/cameras.yaml")
    parser.add_argument("--output")
    args = parser.parse_args()
    try:
        result = TapoOnvifProbe(configured_camera(args.config, args.camera_id)).run()
        rendered = json.dumps(result, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as handle:
                handle.write(rendered + "\n")
        print(rendered)
    except Exception as exc:
        print(f"Tapo ONVIF probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
