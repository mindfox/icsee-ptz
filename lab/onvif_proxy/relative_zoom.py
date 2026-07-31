import io
import math
import os
import threading
import time
import xml.etree.ElementTree as ET

import proxy

RELATIVE_ZOOM_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/TranslationGenericSpace"
ABSOLUTE_ZOOM_SPACE = "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace"
RELATIVE_ZOOM_VELOCITY = float(os.environ.get("RELATIVE_ZOOM_VELOCITY", "0.5"))
ABSOLUTE_ZOOM_FULL_TRAVEL_SECONDS = float(
    os.environ.get("ABSOLUTE_ZOOM_FULL_TRAVEL_SECONDS", "8.0")
)
_request_state = threading.local()
_zoom_state_lock = threading.Lock()
_synthetic_zoom_position = 0.0
_zoom_moving_until = 0.0

_original_ensure_fov_space = proxy.ensure_fov_space
_original_extract_relative_move = proxy.extract_relative_move
_original_soap_payload = proxy.soap_payload
_original_transform_response = proxy.transform_response
_original_forward = proxy.ProxyHandler.forward


def _space_uri(node: ET.Element) -> str:
    return (
        next((u.text for u in node if proxy.local_name(u.tag) == "URI"), "") or ""
    ).strip()


def _insert_before_first(
    parent: ET.Element,
    entry: ET.Element,
    following_names: tuple[str, ...],
) -> None:
    children = list(parent)
    for index, child in enumerate(children):
        if proxy.local_name(child.tag) in following_names:
            parent.insert(index, entry)
            return
    parent.append(entry)


def _add_range_space(
    spaces: ET.Element,
    element_name: str,
    uri: str,
    minimum: str,
    maximum: str,
    following_names: tuple[str, ...],
) -> bool:
    existing = [
        node for node in list(spaces) if proxy.local_name(node.tag) == element_name
    ]
    if any(_space_uri(node) == uri for node in existing):
        return False

    entry = ET.Element(f"{{{proxy.TT}}}{element_name}")
    ET.SubElement(entry, f"{{{proxy.TT}}}URI").text = uri
    x_range = ET.SubElement(entry, f"{{{proxy.TT}}}XRange")
    ET.SubElement(x_range, f"{{{proxy.TT}}}Min").text = minimum
    ET.SubElement(x_range, f"{{{proxy.TT}}}Max").text = maximum
    _insert_before_first(spaces, entry, following_names)
    return True


def _ensure_default_space(
    configuration: ET.Element,
    element_name: str,
    uri: str,
    following_names: tuple[str, ...],
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

    entry = ET.Element(f"{{{proxy.TT}}}{element_name}")
    entry.text = uri
    _insert_before_first(configuration, entry, following_names)
    return True


def ensure_profile_zoom_defaults(root: ET.Element) -> bool:
    changed = False
    for configuration in proxy.find_all(root, "PTZConfiguration"):
        changed = _ensure_default_space(
            configuration,
            "DefaultAbsoluteZoomPositionSpace",
            ABSOLUTE_ZOOM_SPACE,
            (
                "DefaultRelativePanTiltTranslationSpace",
                "DefaultRelativeZoomTranslationSpace",
                "DefaultContinuousPanTiltVelocitySpace",
                "DefaultContinuousZoomVelocitySpace",
                "DefaultPTZSpeed",
                "DefaultPTZTimeout",
                "PanTiltLimits",
                "ZoomLimits",
                "Extension",
            ),
        ) or changed
        changed = _ensure_default_space(
            configuration,
            "DefaultRelativeZoomTranslationSpace",
            RELATIVE_ZOOM_SPACE,
            (
                "DefaultContinuousPanTiltVelocitySpace",
                "DefaultContinuousZoomVelocitySpace",
                "DefaultPTZSpeed",
                "DefaultPTZTimeout",
                "PanTiltLimits",
                "ZoomLimits",
                "Extension",
            ),
        ) or changed
    return changed


def ensure_zoom_spaces(root: ET.Element) -> bool:
    changed = False
    for spaces in proxy.find_all(root, "Spaces") + proxy.find_all(
        root, "SupportedPTZSpaces"
    ):
        changed = _add_range_space(
            spaces,
            "AbsoluteZoomPositionSpace",
            ABSOLUTE_ZOOM_SPACE,
            "0",
            "1",
            (
                "RelativePanTiltTranslationSpace",
                "RelativeZoomTranslationSpace",
                "ContinuousPanTiltVelocitySpace",
                "ContinuousZoomVelocitySpace",
                "PanTiltSpeedSpace",
                "ZoomSpeedSpace",
                "Extension",
            ),
        ) or changed
        changed = _add_range_space(
            spaces,
            "RelativeZoomTranslationSpace",
            RELATIVE_ZOOM_SPACE,
            "-1",
            "1",
            (
                "ContinuousPanTiltVelocitySpace",
                "ContinuousZoomVelocitySpace",
                "PanTiltSpeedSpace",
                "ZoomSpeedSpace",
                "Extension",
            ),
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


def _set_zoom_moving(seconds: float) -> None:
    global _zoom_moving_until
    with _zoom_state_lock:
        _zoom_moving_until = time.monotonic() + max(0.0, seconds)


def _zoom_is_moving() -> bool:
    with _zoom_state_lock:
        return time.monotonic() < _zoom_moving_until


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


def extract_absolute_zoom_move(body: bytes) -> dict | None:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None

    body_nodes = proxy.find_all(root, "Body")
    if not body_nodes or not list(body_nodes[0]):
        return None
    operation = list(body_nodes[0])[0]
    if proxy.local_name(operation.tag) != "AbsoluteMove":
        return None

    profile_nodes = [
        node
        for node in list(operation)
        if proxy.local_name(node.tag) == "ProfileToken"
    ]
    position_nodes = [
        node for node in list(operation) if proxy.local_name(node.tag) == "Position"
    ]
    if not profile_nodes or not position_nodes:
        return None

    zoom_nodes = [
        node
        for node in list(position_nodes[0])
        if proxy.local_name(node.tag) == "Zoom"
    ]
    if not zoom_nodes:
        return None

    profile_token = (profile_nodes[0].text or "").strip()
    target = proxy.parse_float(zoom_nodes[0].attrib.get("x"), float("nan"))
    if not profile_token or math.isnan(target):
        return None

    return {
        "profile_token": profile_token,
        "target": min(1.0, max(0.0, target)),
    }


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


def absolute_move_response() -> bytes:
    envelope = ET.Element(f"{{{proxy.SOAP12}}}Envelope")
    body = ET.SubElement(envelope, f"{{{proxy.SOAP12}}}Body")
    ET.SubElement(body, f"{{{proxy.TPTZ}}}AbsoluteMoveResponse")
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def stop_absolute_zoom_after_pulse(
    path: str,
    profile_token: str,
    delay: float,
    target: float,
    generation: int,
) -> None:
    global _synthetic_zoom_position, _zoom_moving_until

    time.sleep(delay)
    try:
        response = proxy.upstream_post(
            path,
            "Stop",
            proxy.soap_payload(
                "Stop",
                profile_token,
                stop_pan_tilt=False,
                stop_zoom=True,
            ),
        )
        proxy.log(
            f"absolute-zoom-stop profile={profile_token!r} status={response.status_code} "
            f"delay={delay:.3f}s target={target:.6f}"
        )
    except Exception as exc:
        proxy.log(
            f"ERROR absolute-zoom-stop profile={profile_token!r}: "
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        with _zoom_state_lock:
            _synthetic_zoom_position = target
            _zoom_moving_until = 0.0
        proxy.clear_synthetic_move(generation)


def compatibility_forward(self: proxy.ProxyHandler) -> None:
    global _synthetic_zoom_position

    length = int(self.headers.get("Content-Length", "0") or "0")
    incoming = self.rfile.read(length) if length else b""
    absolute = extract_absolute_zoom_move(incoming)

    if absolute is None:
        self.rfile = io.BytesIO(incoming)
        return _original_forward(self)

    path = proxy.urlsplit(self.path).path
    started = time.monotonic()
    target = absolute["target"]
    profile_token = absolute["profile_token"]

    with _zoom_state_lock:
        current = _synthetic_zoom_position
    delta = target - current

    if abs(delta) <= 1e-6:
        self.send_payload(200, absolute_move_response())
        proxy.log(
            f"{self.client_address[0]} POST {path} action=AbsoluteMove "
            f"translated=noop current={current:.6f} target={target:.6f}"
        )
        return

    duration = max(
        proxy.PULSE_MIN_SECONDS,
        abs(delta) * max(0.0, ABSOLUTE_ZOOM_FULL_TRAVEL_SECONDS),
    )
    velocity = math.copysign(
        min(abs(RELATIVE_ZOOM_VELOCITY), 1.0),
        delta,
    )

    try:
        response = proxy.upstream_post(
            path,
            "ContinuousMove",
            proxy.soap_payload(
                "ContinuousMove",
                profile_token,
                zoom=velocity,
            ),
        )
        if response.status_code >= 400:
            self.send_payload(
                response.status_code,
                response.content,
                response.headers.get(
                    "Content-Type", "application/soap+xml; charset=utf-8"
                ),
            )
            proxy.log(
                f"ERROR AbsoluteMove->ContinuousMove upstream={response.status_code} "
                f"current={current:.6f} target={target:.6f}"
            )
            return

        generation = proxy.set_moving_for(duration)
        _set_zoom_moving(duration)
        threading.Thread(
            target=stop_absolute_zoom_after_pulse,
            args=(path, profile_token, duration, target, generation),
            daemon=True,
        ).start()
        self.send_payload(200, absolute_move_response())
        elapsed_ms = round((time.monotonic() - started) * 1000, 1)
        proxy.log(
            f"{self.client_address[0]} POST {path} action=AbsoluteMove "
            f"translated=ContinuousMovePulse current={current:.6f} "
            f"target={target:.6f} velocity={velocity:g} "
            f"pulse_seconds={duration:.3f} upstream={response.status_code} "
            f"elapsed_ms={elapsed_ms}"
        )
    except Exception as exc:
        proxy.clear_synthetic_move()
        payload = f"upstream proxy error: {type(exc).__name__}: {exc}".encode("utf-8")
        try:
            self.send_payload(502, payload, "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass
        proxy.log(
            f"ERROR {self.command} {self.path} AbsoluteMove translation: "
            f"{type(exc).__name__}: {exc}"
        )


def ensure_status_zoom(root: ET.Element) -> bool:
    changed = False
    with _zoom_state_lock:
        zoom_position = _synthetic_zoom_position
    zoom_moving = _zoom_is_moving()

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

    desired_status = "MOVING" if zoom_moving else "IDLE"
    for move_status in proxy.find_all(root, "MoveStatus"):
        zoom_status = [
            node
            for node in list(move_status)
            if proxy.local_name(node.tag) == "Zoom"
        ]
        if zoom_status:
            if (zoom_status[0].text or "").strip() != desired_status:
                zoom_status[0].text = desired_status
                changed = True
        else:
            ET.SubElement(move_status, f"{{{proxy.TT}}}Zoom").text = desired_status
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
proxy.ProxyHandler.forward = compatibility_forward

if __name__ == "__main__":
    proxy.log(
        f"relative_zoom=continuous_pulse relative_space={RELATIVE_ZOOM_SPACE} "
        f"absolute_space={ABSOLUTE_ZOOM_SPACE} range=0..1 "
        f"velocity={RELATIVE_ZOOM_VELOCITY} "
        f"absolute_full_travel_seconds={ABSOLUTE_ZOOM_FULL_TRAVEL_SECONDS}"
    )
    proxy.ThreadingHTTPServer(
        (proxy.LISTEN_HOST, proxy.LISTEN_PORT),
        proxy.ProxyHandler,
    ).serve_forever()
