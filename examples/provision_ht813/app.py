"""生成文件供审阅；不会连接设备或修改 /etc/asterisk。"""

from pathlib import Path

import typer

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

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False)


def build_device(mac: str, address: str, server: str, fxs_password: str, fxo_password: str) -> HT813:

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
        mac=mac,
        address=address,
        network=NetworkConfig(dhcp=True),
        fxs=FxsPort(sip=account("fxs1", fxs_password), local_sip_port=5060, random_sip_port=False),
        fxo=FxoPort(
            sip=account("fxo1", fxo_password),
            local_sip_port=5062,
            random_sip_port=False,
            inbound=FxoInbound(destination="s", server=server, port=5060, rings=2, ring_through_fxs=False),
            outbound=FxoOutbound(stage_method=1),
        ),
    )


@app.command()
def main(
    mac: str = typer.Option(..., help="HT813 MAC"),
    address: str = typer.Option(..., help="HT813 局域网 IP"),
    server: str = typer.Option(..., help="Asterisk 局域网 IP"),
    fxs_password: str = typer.Option(..., prompt=True, hide_input=True, help="FXS SIP 密码"),
    fxo_password: str = typer.Option(..., prompt=True, hide_input=True, help="FXO SIP 密码"),
    ari_password: str = typer.Option(..., prompt=True, hide_input=True, help="ARI 密码"),
    ari_username: str = typer.Option("grandstream", help="ARI 用户名"),
    provisioning_directory: Path = typer.Option(Path("outputs/provisioning"), help="设备 XML 输出目录"),
    asterisk_directory: Path = typer.Option(Path("outputs/asterisk"), help="Asterisk 配置输出目录"),
):
    device = build_device(mac, address, server, fxs_password, fxo_password)
    config = AsteriskConfig(ari_username=ari_username, ari_password=ari_password)
    contents = render_asterisk([device], config)
    ProvisioningStore(provisioning_directory).publish(device)
    directory = asterisk_directory
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        path = directory / name
        with path.open("w", encoding="utf-8") as file:
            path.chmod(0o600)
            file.write(content)


if __name__ == "__main__":
    app()
