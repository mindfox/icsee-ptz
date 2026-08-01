from __future__ import annotations

import xml.etree.ElementTree as ET

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"
TDS = "http://www.onvif.org/ver10/device/wsdl"
FOV_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"

for prefix, uri in (("s", SOAP12), ("tptz", TPTZ), ("tt", TT), ("tds", TDS)):
    ET.register_namespace(prefix, uri)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _envelope(response_name: str):
    envelope = ET.Element(f"{{{SOAP12}}}Envelope")
    body = ET.SubElement(envelope, f"{{{SOAP12}}}Body")
    response = ET.SubElement(body, f"{{{TPTZ}}}{response_name}")
    return envelope, response


def _xml(envelope: ET.Element) -> bytes:
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def synthetic_ptz_response(operation: str) -> bytes | None:
    if operation == "GetServiceCapabilities":
        envelope, response = _envelope("GetServiceCapabilitiesResponse")
        capabilities = ET.SubElement(response, f"{{{TPTZ}}}Capabilities")
        capabilities.set("MoveStatus", "true")
        capabilities.set("StatusPosition", "true")
        return _xml(envelope)
    return None


def advertise_ptz(body: bytes, public_origin: str) -> bytes:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return body
    changed = False
    for capabilities in [node for node in root.iter() if _local(node.tag) == "Capabilities"]:
        if not any(_local(child.tag) == "PTZ" for child in capabilities):
            ptz = ET.SubElement(capabilities, f"{{{TT}}}PTZ")
            ET.SubElement(ptz, f"{{{TT}}}XAddr").text = f"{public_origin}/onvif/service"
            changed = True
    return _xml(root) if changed else body


def advertise_fov_relative(body: bytes) -> bytes:
    """Advertise FOV-relative pan/tilt while preserving native Tapo metadata.

    Frigate requires TranslationSpaceFov during ONVIF discovery. The Tapo C200
    exposes native RelativeMove using TranslationGenericSpace. The listener
    translates received relative vectors into short native ContinuousMove
    pulses, so only the advertised relative space and range need rewriting.

    Real profile/configuration/node tokens, absolute and continuous spaces,
    status coordinates, home support, and presets remain untouched.
    """

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return body

    changed = False

    for node in root.iter():
        name = _local(node.tag)

        if name == "DefaultRelativePanTiltTranslationSpace":
            if node.text != FOV_SPACE:
                node.text = FOV_SPACE
                changed = True
            continue

        if name != "RelativePanTiltTranslationSpace":
            continue

        uri = next((child for child in node if _local(child.tag) == "URI"), None)
        if uri is None:
            uri = ET.SubElement(node, f"{{{TT}}}URI")
        if uri.text != FOV_SPACE:
            uri.text = FOV_SPACE
            changed = True

        for axis_name in ("XRange", "YRange"):
            axis = next((child for child in node if _local(child.tag) == axis_name), None)
            if axis is None:
                axis = ET.SubElement(node, f"{{{TT}}}{axis_name}")

            minimum = next((child for child in axis if _local(child.tag) == "Min"), None)
            maximum = next((child for child in axis if _local(child.tag) == "Max"), None)

            if minimum is None:
                minimum = ET.SubElement(axis, f"{{{TT}}}Min")
            if maximum is None:
                maximum = ET.SubElement(axis, f"{{{TT}}}Max")

            if minimum.text != "-1":
                minimum.text = "-1"
                changed = True
            if maximum.text != "1":
                maximum.text = "1"
                changed = True

    return _xml(root) if changed else body
