"""Typed device configuration; None means leave the device setting untouched."""

from ipaddress import IPv4Address, IPv4Network
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, hide_input_in_errors=True)


class NetworkConfig(ConfigModel):
    dhcp: bool | None = None
    address: IPv4Address | None = None
    netmask: IPv4Address | None = None
    gateway: IPv4Address | None = None
    dns1: IPv4Address | None = None
    dns2: IPv4Address | None = None

    @model_validator(mode="after")
    def static_network(self):
        if self.dhcp is False and (self.address is None or self.netmask is None):
            raise ValueError("静态网络需要 address 和 netmask")
        if self.dhcp is True and any((self.address, self.netmask, self.gateway)):
            raise ValueError("DHCP 不能同时指定静态地址、掩码或网关")
        if self.netmask:
            IPv4Network(f"0.0.0.0/{self.netmask}")
        return self


class SipAccount(ConfigModel):
    server: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: SecretStr
    auth_id: str | None = None
    server_port: int | None = Field(default=None, ge=1, le=65535)
    registration: bool | None = None
    transport: Literal["udp", "tcp", "tls"] | None = None
    codecs: tuple[Literal["ulaw", "alaw"], ...] | None = None
    dtmf: Literal["rfc4733", "inband", "info"] | None = None
    dialplan: str | None = None

    @field_validator("codecs")
    @classmethod
    def codec_count(cls, value):
        if value is not None and (not value or len(value) > 6):
            raise ValueError("codecs 需要 1 至 6 个编码")
        return value

    @property
    def registrar(self) -> str:
        return f"{self.server}:{self.server_port}" if self.server_port else self.server


class FxoInbound(ConfigModel):
    destination: str | None = None
    server: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    rings: int | None = Field(default=None, ge=1, le=50)
    ring_through_fxs: bool | None = None


class FxoOutbound(ConfigModel):
    stage_method: Literal[1, 2] | None = None


class Port(ConfigModel):
    type: Literal["fxs", "fxo"]
    index: Literal[1] = 1
    sip: SipAccount | None = None
    endpoint: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    local_sip_port: int | None = Field(default=None, ge=1, le=65535)
    random_sip_port: bool | None = None

    @property
    def sip_port(self) -> int:
        return self.local_sip_port or (5060 if self.type == "fxs" else 5062)


class FxsPort(Port):
    type: Literal["fxs"] = "fxs"


class FxoPort(Port):
    type: Literal["fxo"] = "fxo"
    inbound: FxoInbound | None = None
    outbound: FxoOutbound | None = None
