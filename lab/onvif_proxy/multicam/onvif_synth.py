from __future__ import annotations

import xml.etree.ElementTree as ET

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
TPTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"
TDS = "http://www.onvif.org/ver10/device/wsdl"
FOV_SPACE = "http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov"

for prefix, uri in (("s", SOAP12), ("tptz", TPTZ), ("tt", TT), ("tds", TDS)):
    ET.register_namespace(prefix, uri)


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
        capabilities.set("StatusPosition", "false")
        return _xml(envelope)
    if operation in {"GetNodes", "GetNode"}:
        envelope, response = _envelope(f"{operation}Response")
        node = ET.SubElement(response, f"{{{TPTZ}}}PTZNode")
        node.set("token", "tapo-ptz-node")
        ET.SubElement(node, f"{{{TT}}}Name").text = "Tapo pan/tilt"
        spaces = ET.SubElement(node, f"{{{TT}}}SupportedPTZSpaces")
        relative = ET.SubElement(spaces, f"{{{TT}}}RelativePanTiltTranslationSpace")
        ET.SubElement(relative, f"{{{TT}}}URI").text = FOV_SPACE
        for axis in ("XRange", "YRange"):
            axis_node = ET.SubElement(relative, f"{{{TT}}}{axis}")
            ET.SubElement(axis_node, f"{{{TT}}}Min").text = "-1"
            ET.SubElement(axis_node, f"{{{TT}}}Max").text = "1"
        ET.SubElement(node, f"{{{TT}}}MaximumNumberOfPresets").text = "0"
        ET.SubElement(node, f"{{{TT}}}HomeSupported").text = "false"
        return _xml(envelope)
    if operation in {"GetConfiguration", "GetConfigurations"}:
        envelope, response = _envelope(f"{operation}Response")
        config = ET.SubElement(response, f"{{{TPTZ}}}PTZConfiguration")
        config.set("token", "tapo-ptz-config")
        ET.SubElement(config, f"{{{TT}}}Name").text = "Tapo PTZ"
        ET.SubElement(config, f"{{{TT}}}UseCount").text = "1"
        ET.SubElement(config, f"{{{TT}}}NodeToken").text = "tapo-ptz-node"
        ET.SubElement(config, f"{{{TT}}}DefaultRelativePanTiltTranslationSpace").text = FOV_SPACE
        return _xml(envelope)
    if operation == "GetConfigurationOptions":
        envelope, response = _envelope("GetConfigurationOptionsResponse")
        options = ET.SubElement(response, f"{{{TPTZ}}}PTZConfigurationOptions")
        spaces = ET.SubElement(options, f"{{{TT}}}Spaces")
        relative = ET.SubElement(spaces, f"{{{TT}}}RelativePanTiltTranslationSpace")
        ET.SubElement(relative, f"{{{TT}}}URI").text = FOV_SPACE
        for axis in ("XRange", "YRange"):
            axis_node = ET.SubElement(relative, f"{{{TT}}}{axis}")
            ET.SubElement(axis_node, f"{{{TT}}}Min").text = "-1"
            ET.SubElement(axis_node, f"{{{TT}}}Max").text = "1"
        return _xml(envelope)
    if operation == "GetStatus":
        envelope, response = _envelope("GetStatusResponse")
        status = ET.SubElement(response, f"{{{TPTZ}}}PTZStatus")
        move = ET.SubElement(status, f"{{{TT}}}MoveStatus")
        ET.SubElement(move, f"{{{TT}}}PanTilt").text = "IDLE"
        ET.SubElement(move, f"{{{TT}}}Zoom").text = "IDLE"
        ET.SubElement(status, f"{{{TT}}}UtcTime").text = "1970-01-01T00:00:00Z"
        return _xml(envelope)
    if operation == "GetPresets":
        envelope, _ = _envelope("GetPresetsResponse")
        return _xml(envelope)
    return None


def advertise_ptz(body: bytes, public_origin: str) -> bytes:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return body
    changed = False
    for capabilities in [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Capabilities"]:
        if not any(child.tag.rsplit("}", 1)[-1] == "PTZ" for child in capabilities):
            ptz = ET.SubElement(capabilities, f"{{{TT}}}PTZ")
            ET.SubElement(ptz, f"{{{TT}}}XAddr").text = f"{public_origin}/onvif/ptz_service"
            changed = True
    return _xml(root) if changed else body
