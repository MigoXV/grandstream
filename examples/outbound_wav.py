"""显式外呼指定号码并播放 WAV；需要已完成 HT813 一阶段拨号配置。"""

import asyncio
import os

from provision_ht813 import build_device

from grandstream import AriApplication, AriConfig, WavSource


async def main():
    device = build_device()
    app = AriApplication("grandstream-outbound", ari=AriConfig.from_env(), devices=[device])
    async with app:
        async with await app.dial(device.fxo, number=os.environ["CALL_NUMBER"]) as call:
            # Consume inbound media concurrently, even in a playback-only application.
            async def receive():
                async for _ in call.audio:
                    pass

            receiver = asyncio.create_task(receive())
            try:
                await call.audio.play(WavSource(os.environ["CALL_WAV"]))
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
