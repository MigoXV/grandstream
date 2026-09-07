"""Generate reviewable Asterisk include files for registered HT813 ports on a LAN."""

import re
from collections.abc import Sequence
from ipaddress import IPv4Address

from pydantic import Field, SecretStr

from grandstream.models import HT813
from grandstream.models.config import ConfigModel


class AsteriskConfig(ConfigModel):
    app: str = Field(default="grandstream", pattern=r"^[A-Za-z0-9_-]+$")
    sip_bind: IPv4Address = IPv4Address("0.0.0.0")
    sip_port: int = Field(default=5060, ge=1, le=65535)
    http_bind: IPv4Address = IPv4Address("127.0.0.1")
    http_port: int = Field(default=8088, ge=1, le=65535)
    ari_username: str = Field(default="grandstream", pattern=r"^[A-Za-z0-9_-]+$")
    ari_password: SecretStr


def _value(value: str) -> str:
    # Asterisk's config grammar is not XML. Reject constructs that could alter a section.
    if not value or any(c in value for c in "\r\n\x00;[]\\"):
        raise ValueError("Asterisk 配置值不能为空，或包含换行、分号、方括号、反斜杠")
    return value


def render_asterisk(devices: Sequence[HT813], config: AsteriskConfig) -> dict[str, str]:
    """Return file contents. Never install, reload, or replace system configuration."""
    transport = f"{config.app}-udp"
    pjsip = [f"[{transport}]\ntype=transport\nprotocol=udp\nbind={config.sip_bind}:{config.sip_port}\n"]
    dialplan = []
    names = set()
    matches = set()
    for device in devices:
        for port in device.ports:
            sip = port.sip
            if sip is None:
                continue
            if device.address is None:
                raise ValueError("生成 PJSIP identify 需要设备 address（可使用 DHCP 地址保留）")
            if sip.registration is False or sip.transport not in (None, "udp"):
                raise ValueError("首版 Asterisk 生成器面向注册型 UDP 接入")
            if port.random_sip_port:
                raise ValueError("按来源端口识别时必须关闭随机 SIP 端口")
            endpoint = device.endpoint_name(port)
            # REGISTER's To user selects the AOR independently of the endpoint name.
            aor = _value(sip.username)
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", aor):
                raise ValueError("注册用户名只能包含字母、数字、点、下划线和连字符")
            match = f"{device.address}:{port.sip_port}"
            if endpoint in names or aor in names or match in matches:
                raise ValueError("endpoint、注册用户名或设备来源端口重复")
            names.update((endpoint, aor))
            matches.add(match)
            context = f"{config.app}-from-{endpoint}"
            auth = f"{endpoint}-auth"
            codecs = ",".join(sip.codecs or ("alaw", "ulaw"))
            dtmf = {"rfc4733": "rfc4733", "inband": "inband", "info": "info"}[sip.dtmf or "rfc4733"]
            pjsip.append(
                f"[{endpoint}]\ntype=endpoint\ntransport={transport}\ncontext={context}\n"
                f"disallow=all\nallow={codecs}\ndtmf_mode={dtmf}\naors={aor}\nauth={auth}\n"
                "direct_media=no\nrtp_symmetric=yes\nforce_rport=yes\nrewrite_contact=yes\n"
                f"\n[{auth}]\ntype=auth\nauth_type=userpass\n"
                f"username={_value(sip.auth_id or sip.username)}\npassword={_value(sip.password.get_secret_value())}\n"
                f"\n[{aor}]\ntype=aor\nmax_contacts=1\nremove_existing=yes\nqualify_frequency=30\n"
                f"\n[{endpoint}-identify]\ntype=identify\nendpoint={endpoint}\nmatch={match}\n"
            )
            dialplan.append(
                f"[{context}]\nexten => s,1,Stasis({config.app},{endpoint})\n same => n,Hangup()\n"
                "exten => _.,1,Goto(s,1)\n"
            )
    return {
        "pjsip-grandstream.conf": "\n".join(pjsip),
        "extensions-grandstream.conf": "\n".join(dialplan),
        "ari-grandstream.conf": (
            f"[general]\nenabled=yes\n\n[{config.ari_username}]\ntype=user\nread_only=no\n"
            f"password={_value(config.ari_password.get_secret_value())}\n"
        ),
        "http-grandstream.conf": (
            f"[general]\nenabled=yes\nbindaddr={config.http_bind}\nbindport={config.http_port}\n"
        ),
        "chan_websocket-grandstream.conf": "[global]\ncontrol_message_format=json\n",
    }
