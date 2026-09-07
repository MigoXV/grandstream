"""生成文件供审阅；不会连接设备或修改 /etc/asterisk。"""

import os
from pathlib import Path

from grandstream import (
    HT813,
    AsteriskConfig,
    FxoInbound,
    FxoOutbound,
    FxoPort,
    FxsPort,
    NetworkConfig,
    ProvisioningStore,
    SipAccount,
    render_asterisk,
)


def build_device() -> HT813:
    server = os.environ["ASTERISK_LAN_IP"]

    def account(username: str, password: str):
        return SipAccount(
            server=server,
            username=username,
            auth_id=username,
            password=password,
            registration=True,
            transport="udp",
            codecs=("alaw", "ulaw"),
            dtmf="rfc4733",
        )

    return HT813(
        mac=os.environ["HT813_MAC"],
        address=os.environ["HT813_IP"],
        network=NetworkConfig(dhcp=True),
        fxs=FxsPort(
            sip=account("fxs1", os.environ["HT813_FXS_PASSWORD"]), local_sip_port=5060, random_sip_port=False
        ),
        fxo=FxoPort(
            sip=account("fxo1", os.environ["HT813_FXO_PASSWORD"]),
            local_sip_port=5062,
            random_sip_port=False,
            inbound=FxoInbound(destination="s", server=server, port=5060, rings=2, ring_through_fxs=False),
            outbound=FxoOutbound(stage_method=1),
        ),
    )


def main():
    device = build_device()
    ProvisioningStore("outputs/provisioning").publish(device)
    config = AsteriskConfig(ari_password=os.environ["GRANDSTREAM_ARI_PASSWORD"])
    directory = Path("outputs/asterisk")
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in render_asterisk([device], config).items():
        path = directory / name
        with path.open("w", encoding="utf-8") as file:
            path.chmod(0o600)
            file.write(content)


if __name__ == "__main__":
    main()
