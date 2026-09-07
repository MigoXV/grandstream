"""Command-line diagnostics, provisioning server and PCM echo demonstration."""

import asyncio
import logging
from pathlib import Path

import typer

from grandstream.ari import AriApplication, AriClient, AriConfig
from grandstream.errors import GrandstreamError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger(__name__)
app = typer.Typer(help="Grandstream HT813 / Asterisk ARI SDK", pretty_exceptions_enable=False)
provision = typer.Typer(help="HTTP XML provisioning")
audio = typer.Typer(help="语音流示例")
app.add_typer(provision, name="provision")
app.add_typer(audio, name="audio")


def _run(coroutine):
    try:
        return asyncio.run(coroutine)
    except (GrandstreamError, ValueError, asyncio.TimeoutError) as exc:
        logger.error("%s", exc)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        return None


@app.command()
def doctor():
    """只读检查 ARI、Asterisk 版本及 WebSocket 模块。凭据来自 GRANDSTREAM_ARI_*。"""

    async def check():
        async with AriClient(AriConfig.from_env()) as client:
            info = await client.request("GET", "asterisk/info")
            modules = await client.request("GET", "asterisk/modules")
            names = {item["name"].removesuffix(".so") for item in modules}
            required = {"chan_websocket", "res_http_websocket", "res_ari_channels", "res_ari_events"}
            missing = required - names
            logger.info("Asterisk %s；ARI 可用", info.get("system", {}).get("version", "unknown"))
            if missing:
                raise GrandstreamError("缺少模块: " + ", ".join(sorted(missing)))
            logger.info("媒体模块齐全；首版验收基线为 23.5.0，实际音频请运行独立模拟通话测试")

    _run(check())


@provision.command("serve")
def serve(
    directory: Path = typer.Option(
        Path("outputs/provisioning"),
        "--directory",
        envvar="GRANDSTREAM_PROVISION_DIRECTORY",
        help="已发布 XML 的目录",
    ),
    host: str = typer.Option("127.0.0.1", "--host", envvar="GRANDSTREAM_PROVISION_HOST"),
    port: int = typer.Option(8000, "--port", envvar="GRANDSTREAM_PROVISION_PORT", min=1, max=65535),
):
    """托管已发布配置；局域网设备访问时显式设置 --host。"""
    try:
        import uvicorn

        from grandstream.provisioning.server import create_app

        asgi = create_app(directory)
    except ImportError:
        logger.error("请安装 HTTP 可选依赖：poetry install -E provisioning")
        raise typer.Exit(1) from None
    uvicorn.run(asgi, host=host, port=port)


@audio.command("echo")
def echo(
    name: str = typer.Option("grandstream", "--app", envvar="GRANDSTREAM_ARI_APP"),
    media_sample_rate: int = typer.Option(
        8000, "--media-sample-rate", envvar="GRANDSTREAM_MEDIA_SAMPLE_RATE"
    ),
    sample_rate: int | None = typer.Option(
        None,
        "--sample-rate",
        envvar="GRANDSTREAM_RECEIVE_SAMPLE_RATE",
        help="接收目标采样率；省略时保留原始采样率",
    ),
):
    """接听进入此 Stasis 应用的电话并回送 PCM。"""

    async def run():
        application = AriApplication(name, ari=AriConfig.from_env(), media_sample_rate=media_sample_rate)

        @application.incoming_call
        async def incoming(call):
            await call.answer()
            logger.info("接听 call=%s endpoint=%s", call.id, call.endpoint)
            async for frame in call.audio.receive(sample_rate=sample_rate):
                await call.audio.write(frame)

        await application.run()

    _run(run())


if __name__ == "__main__":
    app()
