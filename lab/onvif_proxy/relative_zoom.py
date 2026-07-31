import math
import os
import threading
import xml.etree.ElementTree as ET

import proxy

RELATIVE_ZOOM_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationGenericSpace"
ABSOLUTE_ZOOM_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"
RELATIVE_ZOOM_VELOCITY = float(os.environ.get("RELATIVE_ZOOM_VELOCITY", "0.5"))
_request_state = threading.local()
_zoom_state_lock = threading.Lock()
_synthetic_zoom_position = 0.0

_original_ensure_fov_space = proxy.ensure_fov_space
_original_extract_relative_move = proxy.extract_relative_move
_original_soap_payload = proxy.soap_payload
_original_transform_response = proxy.transform_response

PTZ_CONFIGURATION_ORDER = [
    "NodeToken",
    "DefaultAbsolutePantTiltPositionSpace",
    "DefaultAbsoluteZoomPositionSpace",
    "DefaultRelativePanTiltTranslationSpace",
    "DefaultRelativeZoomTranslationSpace",
    "DefaultContinuousPanTiltVelocitySpace",
    "DefaultContinuousZoomVelocitySpace",
    "DefaultPTZSpeed",
    "DefaultPTZTimeout",
    "PanTiltLimits",
    "ZoomLimits",
    "Extension",
]

PTZ_SPACES_ORDER = [
    "AbsolutePanTiltPositionSpace",
    "AbsoluteZoomPositionSpace",
    "RelativePanTiltTranslationSpace",
    "RelativeZoomTranslationSpace",
    "ContinuousPanTiltVelocitySpace",
    "ContinuousZoomVelocitySpace",
    "PanTiltSpeedSpace",
    "ZoomSpeedSpace",
    "Extension",
]


def _space_uri(node: ET.Element) -> str:
    return (next((u.text for u in node if proxy.local_name(u.tag) == "URI"), "") or "").strip()


def _insert_in_schema_order(
    parent: ET.Element,
    child: ET.Element,
    order: list[str],
) -> None:
    child_name = proxy.local_name(child.tag)
    try:
        child_rank = order.index(child_name)
    except ValueError:
        parent.append(child)
        return

    siblings = list(parent)
    for index, sibling in enumerate(siblings):
        sibling_name = proxy.local_name(sibling.tag)
        try:
            sibling_rank = order.index(sibling_name)
        except ValueError:
            continue
        if sibling_rank > child_rank:
            parent.insert(index, child)
            return
    parent.append(child)


def _add_range_space(
    spaces: ET.Element,
    element_name: str,
    uri: str,
    minimum: str,
    maximum: str,
) -> bool:
    existing = [node for node in list(spaces) if proxy.local_name(node.tag) == element_name]
    if any(_space_uri(node) == uri for node in existing):
        return False

    entry = ET.Element(f"{{{proxy.TT}}}{element_name}")
    ET.SubElement(entry, f"{{{proxy.TT}}}URI").text = uri
    x_range = ET.SubElement(entry, f"{{{proxy.TT}}}XRange")
    ET.SubElement(x_range, f"{{{proxy.TT}}}Min").text = minimum
    ET.SubElement(x_range, f"{{{proxy.TT}}}Max").text = maximum
    _insert_in_schema_order(spaces, entry, PTZ_SPACES_ORDER)
    return True


def _ensure_default_space(
    configuration: ET.Element,
    element_name: str,
    uri: str,
) -> bool:
    existing = [
        node
        for node in list(configuration)
        if proxy.local_name(node.tag) == element_name
    ]
    if existing:
        changed = False
        for node in existing:
            if (node.text or "").strip() != uri:
                node.text = uri
                changed = True
        return changed

    node = ET.Element(f"{{{proxy.TT}}}{element_name}")
    node.text = uri
    _insert_in_schema_order(configuration, node, PTZ_CONFIGURATION_ORDER)
    return True


def ensure_profile_zoom_defaults(root: ET.Element) -> bool:
    changed = False
    for configuration in proxy.find_all(root, "PTZConfiguration"):
        changed = _ensure_default_space(
            configuration,
            "DefaultAbsoluteZoomPositionSpace",
            ABSOLUTE_ZOOM_SPACE,
        ) or changed
        changed = _ensure_default_space(
            configuration,
            "DefaultRelativeZoomTranslationSpace",
            RELATIVE_ZOOM_SPACE,
        ) or changed
    return changed


def ensure_zoom_spaces(root: ET.Element) -> bool:
    changed = False
    seen: set[int] = set()
    for spaces in proxy.find_all(root, "Spaces") + proxy.find_all(root, "SupportedPTZSpaces"):
        if id(spaces) in seen:
            continue
        seen.add(id(spaces))
        changed = _add_range_space(
            spaces,
            "AbsoluteZoomPositionSpace",
            ABSOLUTE_ZOOM_SPACE,
            "0",
            "1",
        ) or changed
        changed = _add_range_space(
            spaces,
            "RelativeZoomTranslationSpace",
            RELATIVE_ZOOM_SPACE,
            "-1",
            "1",
        ) or changed

    for node in proxy.find_all(root, "DefaultRelativeZoomTranslationSpace"):
        if (node.text or "").strip() != RELATIVE_ZOOM_SPACE:
            node.text = RELATIVE_ZOOM_SPACE
            changed = True

    for node in proxy.find_all(root, "DefaultAbsoluteZoomPositionSpace"):
        if (node.text or "").strip() != ABSOLUTE_ZOOM_SPACE:
            node.text = ABSOLUTE_ZOOM_SPACE
            changed = True

    return changed


def ensure_fov_and_zoom_spaces(root: ET.Element) -> bool:
    changed = _original_ensure_fov_space(root)
    changed = ensure_zoom_spaces(root) or changed
    changed = ensure_profile_zoom_defaults(root) or changed
    return changed


def extract_relative_move(body: bytes) -> dict | None:
    global _synthetic_zoom_position

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
                with _zoom_state_lock:
                    _synthetic_zoom_position = min(
                        1.0,
                        max(0.0, _synthetic_zoom_position + zoom_value),
                    )
            break
    return relative


def pulse_duration(x: float, y: float) -> float:
    magnitude = max(
        abs(x),
        abs(y),
        abs(getattr(_request_state, "zoom_value", 0.0)),
    )
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


def ensure_status_zoom(root: ET.Element) -> bool:
    changed = False
    with _zoom_state_lock:
        zoom_position = _synthetic_zoom_position

    position_nodes = proxy.find_all(root, "Position")
    if not position_nodes:
        status_nodes = proxy.find_all(root, "PTZStatus")
        if not status_nodes:
            return False
        position_nodes = [ET.SubElement(status_nodes[0], f"{{{proxy.TT}}}Position")]
        changed = True

    position = position_nodes[0]
    zoom_nodes = [
        node for node in list(position) if proxy.local_name(node.tag) == "Zoom"
    ]
    if zoom_nodes:
        zoom = zoom_nodes[0]
    else:
        zoom = ET.SubElement(position, f"{{{proxy.TT}}}Zoom")
        changed = True

    desired_x = f"{zoom_position:.6f}"
    if zoom.attrib.get("x") != desired_x:
        zoom.set("x", desired_x)
        changed = True
    if zoom.attrib.get("space") != ABSOLUTE_ZOOM_SPACE:
        zoom.set("space", ABSOLUTE_ZOOM_SPACE)
        changed = True

    for move_status in proxy.find_all(root, "MoveStatus"):
        zoom_status = [
            node
            for node in list(move_status)
            if proxy.local_name(node.tag) == "Zoom"
        ]
        if not zoom_status:
            ET.SubElement(move_status, f"{{{proxy.TT}}}Zoom").text = "IDLE"
            changed = True

    return changed


def transform_response(body: bytes, origin: str, action: str) -> bytes:
    transformed = _original_transform_response(body, origin, action)
    if not transformed:
        return transformed

    try:
        root = ET.fromstring(transformed)
    except ET.ParseError:
        return transformed

    changed = False
    if action in {
        "GetProfiles",
        "GetProfile",
        "GetConfigurations",
        "GetConfiguration",
    }:
        changed = ensure_profile_zoom_defaults(root) or changed
    if action in {"GetNode", "GetConfiguration", "GetConfigurationOptions"}:
        changed = ensure_zoom_spaces(root) or changed
    if action == "GetStatus":
        changed = ensure_status_zoom(root) or changed

    return (
        ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if changed
        else transformed
    )


proxy.ensure_fov_space = ensure_fov_and_zoom_spaces
proxy.extract_relative_move = extract_relative_move
proxy.pulse_duration = pulse_duration
proxy.soap_payload = soap_payload
proxy.transform_response = transform_response

if __name__ == "__main__":
    proxy.log(
        f"relative_zoom=continuous_pulse relative_space={RELATIVE_ZOOM_SPACE} "
        f"absolute_space={ABSOLUTE_ZOOM_SPACE} range=0..1 "
        f"velocity={RELATIVE_ZOOM_VELOCITY}"
    )
    proxy.ThreadingHTTPServer(
        (proxy.LISTEN_HOST, proxy.LISTEN_PORT),
        proxy.ProxyHandler,
    ).serve_forever()
