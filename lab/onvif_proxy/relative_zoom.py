import math
import os
import threading
import xml.etree.ElementTree as ET

import proxy

RELATIVE_ZOOM_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationGenericSpace"
RELATIVE_ZOOM_VELOCITY = float(os.environ.get("RELATIVE_ZOOM_VELOCITY", "0.5"))
_request_state = threading.local()

_original_ensure_fov_space = proxy.ensure_fov_space
_original_extract_relative_move = proxy.extract_relative_move
_original_soap_payload = proxy.soap_payload


def ensure_relative_zoom_space(root: ET.Element) -> bool:
    changed = False
    for spaces in proxy.find_all(root, "Spaces") + proxy.find_all(root, "SupportedPTZSpaces"):
        existing = [node for node in list(spaces) if proxy.local_name(node.tag) == "RelativeZoomTranslationSpace"]
        if not any(
            (next((u.text for u in node if proxy.local_name(u.tag) == "URI"), "") or "").strip()
            == RELATIVE_ZOOM_SPACE
            for node in existing
        ):
            entry = ET.Element(f"{{{proxy.TT}}}RelativeZoomTranslationSpace")
            ET.SubElement(entry, f"{{{proxy.TT}}}URI").text = RELATIVE_ZOOM_SPACE
            x_range = ET.SubElement(entry, f"{{{proxy.TT}}}XRange")
            ET.SubElement(x_range, f"{{{proxy.TT}}}Min").text = "-1"
            ET.SubElement(x_range, f"{{{proxy.TT}}}Max").text = "1"
            spaces.append(entry)
            changed = True

    for node in proxy.find_all(root, "DefaultRelativeZoomTranslationSpace"):
        if (node.text or "").strip() != RELATIVE_ZOOM_SPACE:
            node.text = RELATIVE_ZOOM_SPACE
            changed = True

    return changed


def ensure_fov_and_zoom_space(root: ET.Element) -> bool:
    changed = _original_ensure_fov_space(root)
    return ensure_relative_zoom_space(root) or changed


def extract_relative_move(body: bytes) -> dict | None:
    _request_state.zoom_value = 0.0
    _request_state.zoom_velocity = None
    relative = _original_extract_relative_move(body)
    if relative is None:
        return None

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return relative

    for zoom in proxy.find_all(root, "Zoom"):
        if zoom.attrib.get("space") in (None, "", RELATIVE_ZOOM_SPACE):
            zoom_value = proxy.parse_float(zoom.attrib.get("x"))
            if abs(zoom_value) > 1e-9:
                _request_state.zoom_value = zoom_value
                _request_state.zoom_velocity = math.copysign(
                    min(abs(RELATIVE_ZOOM_VELOCITY), 1.0), zoom_value
                )
            break
    return relative


def pulse_duration(x: float, y: float) -> float:
    magnitude = max(abs(x), abs(y), abs(getattr(_request_state, "zoom_value", 0.0)))
    if magnitude <= 1e-9:
        return 0.0
    return max(
        proxy.PULSE_MIN_SECONDS,
        min(
            proxy.PULSE_MAX_SECONDS,
            proxy.PULSE_MIN_SECONDS + magnitude * proxy.PULSE_SECONDS_PER_FOV,
        ),
    )


def soap_payload(operation: str, profile_token: str, **kwargs) -> bytes:
    if operation == "ContinuousMove" and "zoom" not in kwargs:
        zoom_velocity = getattr(_request_state, "zoom_velocity", None)
        if zoom_velocity is not None:
            kwargs["zoom"] = zoom_velocity
            _request_state.zoom_velocity = None
            _request_state.zoom_value = 0.0
    return _original_soap_payload(operation, profile_token, **kwargs)


proxy.ensure_fov_space = ensure_fov_and_zoom_space
proxy.extract_relative_move = extract_relative_move
proxy.pulse_duration = pulse_duration
proxy.soap_payload = soap_payload

if __name__ == "__main__":
    proxy.log(
        f"relative_zoom=continuous_pulse space={RELATIVE_ZOOM_SPACE} "
        f"velocity={RELATIVE_ZOOM_VELOCITY}"
    )
    proxy.ThreadingHTTPServer((proxy.LISTEN_HOST, proxy.LISTEN_PORT), proxy.ProxyHandler).serve_forever()
