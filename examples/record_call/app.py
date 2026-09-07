"""接收流写入 WAV；原生采样率由首帧决定。"""

import asyncio
from pathlib import Path

import typer

from grandstream import AriApplication, AriConfig, WavSink

app = typer.Typer(help=__doc__, pretty_exceptions_enable=False)


async def run(config: AriConfig, directory: Path, app_name: str):
    application = AriApplication(app_name, ari=config)
    directory.mkdir(parents=True, exist_ok=True)

    @application.incoming_call
    async def incoming(call):
        await call.answer()
        async with WavSink(directory / f"{call.id.replace('/', '_')}.wav") as sink:
            async for frame in call.audio.receive():
                await sink.write(frame)

    await application.run()


@app.command()
def main(
    directory: Path = typer.Option(Path("outputs/recordings"), help="录音输出目录"),
    app_name: str = typer.Option("grandstream-record", help="与 dialplan 中 Stasis 名称一致"),
    ari_url: str = typer.Option("http://127.0.0.1:8088/ari", help="ARI REST 地址"),
    ari_username: str = typer.Option("grandstream", help="ARI 用户名"),
    ari_password: str = typer.Option(..., prompt=True, hide_input=True, help="ARI 密码；未传时隐藏输入"),
    media_url: str | None = typer.Option(None, help="独立媒体服务地址，默认由 ARI 地址推导"),
):
    config = AriConfig(url=ari_url, username=ari_username, password=ari_password, media_url=media_url)
    asyncio.run(run(config, directory, app_name))


if __name__ == "__main__":
    app()
