"""接收流写入 WAV；原生采样率由首帧决定。"""

import asyncio
from pathlib import Path

from grandstream import AriApplication, AriConfig, WavSink


async def main():
    application = AriApplication("grandstream-record", ari=AriConfig.from_env())
    directory = Path("outputs/recordings")
    directory.mkdir(parents=True, exist_ok=True)

    @application.incoming_call
    async def incoming(call):
        await call.answer()
        async with WavSink(directory / f"{call.id.replace('/', '_')}.wav") as sink:
            async for frame in call.audio.receive():
                await sink.write(frame)

    await application.run()


if __name__ == "__main__":
    asyncio.run(main())
