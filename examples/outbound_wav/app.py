"""显式外呼指定号码并播放 WAV；需要已完成 HT813 一阶段拨号配置。"""

import asyncio
from pathlib import Path

import typer

from grandstream import HT813, AriApplication, AriConfig, FxoOutbound, FxoPort, WavSource

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False)


async def run(config: AriConfig, mac: str, endpoint: str | None, number: str, wav: Path):
    device = HT813(mac=mac, fxo=FxoPort(endpoint=endpoint, outbound=FxoOutbound(stage_method=1)))
    app = AriApplication("grandstream-outbound", ari=config, devices=[device])
    async with app:
        async with await app.dial(device.fxo, number=number) as call:
            # Consume inbound media concurrently, even in a playback-only application.
            async def receive():
                async for _ in call.audio:
                    pass

            receiver = asyncio.create_task(receive())
            try:
                await call.audio.play(WavSource(wav))
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)


@app.command()
def main(
    mac: str = typer.Option(..., help="HT813 MAC，用于默认 endpoint 名称"),
    endpoint: str | None = typer.Option(None, help="已配置的 FXO PJSIP endpoint，默认由 MAC 推导"),
    number: str = typer.Option(..., help="实际拨打的电话号码"),
    wav: Path = typer.Option(..., exists=True, dir_okay=False, readable=True, help="播放的 WAV 文件"),
    ari_url: str = typer.Option("http://127.0.0.1:8088/ari", help="ARI REST 地址"),
    ari_username: str = typer.Option("grandstream", help="ARI 用户名"),
    ari_password: str = typer.Option(..., prompt=True, hide_input=True, help="ARI 密码；未传时隐藏输入"),
    media_url: str | None = typer.Option(None, help="独立媒体服务地址，默认由 ARI 地址推导"),
):
    config = AriConfig(url=ari_url, username=ari_username, password=ari_password, media_url=media_url)
    asyncio.run(run(config, mac, endpoint, number, wav))


if __name__ == "__main__":
    app()
