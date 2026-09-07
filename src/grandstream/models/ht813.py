"""HT813 identity and port capabilities, independent of ARI."""

import re
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import ClassVar

from pydantic import Field, field_validator

from .config import ConfigModel, FxoPort, FxsPort, NetworkConfig, Port


def normalize_mac(value: str) -> str:
    value = value.replace(":", "").replace("-", "").lower()
    if not re.fullmatch(r"[0-9a-f]{12}", value):
        raise ValueError("MAC 必须是 12 位十六进制地址")
    return value


@dataclass(frozen=True)
class DeviceCapabilities:
    fxs_ports: int
    fxo_ports: int
    sip_profiles: int
    xml_provisioning: bool


class HT813(ConfigModel):
    model: ClassVar[str] = "HT813"
    capabilities: ClassVar[DeviceCapabilities] = DeviceCapabilities(1, 1, 2, True)
    mac: str
    address: IPv4Address | None = None
    network: NetworkConfig | None = None
    fxs: FxsPort = Field(default_factory=FxsPort)
    fxo: FxoPort = Field(default_factory=FxoPort)

    _normalize_mac = field_validator("mac")(normalize_mac)

    @property
    def ports(self) -> tuple[FxsPort, FxoPort]:
        return self.fxs, self.fxo

    def endpoint_name(self, port: Port) -> str:
        if not any(port is item for item in self.ports):
            raise ValueError("端口不属于此设备")
        return port.endpoint or f"ht813-{self.mac}-{port.type}{port.index}"

    def render_xml(self) -> bytes:
        from grandstream.provisioning.xml import render_xml

        return render_xml(self)
