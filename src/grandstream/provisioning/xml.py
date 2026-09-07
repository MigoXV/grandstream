"""Grandstream XML serialization with the complete provisioning envelope."""

import re
from xml.etree import ElementTree as ET

from grandstream.models import HT813
from grandstream.models.ht813 import normalize_mac

from .params import parameters


def configuration_xml(values: dict[str, str], mac: str | None = None) -> bytes:
    root = ET.Element("gs_provision", version="1")
    if mac is not None:
        ET.SubElement(root, "mac").text = normalize_mac(mac)
    config = ET.SubElement(root, "config", version="1")
    for key, value in sorted(
        values.items(), key=lambda item: int(item[0][1:]) if item[0][1:].isdigit() else 0
    ):
        if not re.fullmatch(r"P[0-9]+", key):
            raise ValueError("配置参数名必须是 P-value")
        if any(
            not (
                c in "\t\n\r"
                or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD
                or 0x10000 <= ord(c) <= 0x10FFFF
            )
            for c in value
        ):
            raise ValueError("配置包含 XML 1.0 不支持的字符")
        ET.SubElement(config, key).text = value
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def render_xml(device: HT813) -> bytes:
    return configuration_xml(parameters(device), device.mac)
