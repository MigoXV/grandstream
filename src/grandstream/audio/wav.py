"""WAV I/O with format metadata and bounded chunks; disk I/O runs off the event loop."""

import asyncio
import wave
from pathlib import Path

from .pcm import AudioFrame, validate_rate


class WavSource:
    def __init__(self, path: str | Path, *, chunk_ms: int = 20):
        if chunk_ms <= 0:
            raise ValueError("chunk_ms 必须为正数")
        self.path, self.chunk_ms = Path(path), chunk_ms

    async def __aiter__(self):
        file = await asyncio.to_thread(wave.open, str(self.path), "rb")
        try:
            if file.getnchannels() != 1 or file.getsampwidth() != 2 or file.getcomptype() != "NONE":
                raise ValueError("WAV 必须为单声道 PCM16")
            rate = validate_rate(file.getframerate())
            while data := await asyncio.to_thread(file.readframes, max(1, rate * self.chunk_ms // 1000)):
                yield AudioFrame(data, rate)
        finally:
            await asyncio.to_thread(file.close)


class WavSink:
    """Single-writer WAV sink. The first frame determines the sample rate."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file = None
        self._rate = None
        self._closed = False

    async def write(self, frame: AudioFrame):
        if self._closed:
            raise ValueError("WAV 已关闭")
        if self._file is None:
            self._file = await asyncio.to_thread(wave.open, str(self.path), "wb")
            self._rate = frame.sample_rate
            self._file.setparams((1, 2, self._rate, 0, "NONE", "not compressed"))
        if frame.sample_rate != self._rate:
            raise ValueError("同一 WAV 的采样率不能变化")
        await asyncio.to_thread(self._file.writeframes, frame.pcm)

    async def close(self):
        self._closed = True
        if self._file:
            await asyncio.to_thread(self._file.close)
            self._file = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
