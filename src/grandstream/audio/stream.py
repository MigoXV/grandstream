"""Full duplex chan_websocket PCM transport with bounded playback and cancellation."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterable

from grandstream.errors import AudioOverflow, CallClosed, MediaError, PlaybackInterrupted

from .pcm import MEDIA_FORMATS, AudioFrame, Resampler, validate_rate


class AudioStream:
    """One receiver and one output producer per stream; clear() may run concurrently."""

    def __init__(
        self,
        *,
        sample_rate: int = 8000,
        receive_buffer_seconds: float = 2,
        playback_window_ms: int = 200,
        timeout: float = 10,
    ):
        if sample_rate not in MEDIA_FORMATS:
            raise ValueError("媒体连接采样率必须对应 Asterisk slin 格式")
        if receive_buffer_seconds <= 0 or playback_window_ms <= 0 or timeout <= 0:
            raise ValueError("缓冲区、窗口和超时必须为正数")
        self.sample_rate = sample_rate
        self.timeout = timeout
        self._receive_limit = int(receive_buffer_seconds * sample_rate * 2)
        self._window_ms = playback_window_ms
        self._socket = None
        self._reader = None
        self._closed = asyncio.Event()
        self._changed = asyncio.Event()
        self._input_changed = asyncio.Event()
        self._input: deque[bytes] = deque()
        self._input_bytes = 0
        self._receiving = False
        self._error: Exception | None = None
        self._xoff = False
        self._marks: dict[str, None] = {}
        self._sequence = 0
        self._generation = 0
        self._wire = bytearray()
        self._resampler: Resampler | None = None
        self._frame_size = 0
        self._window = 1
        self._send_lock = asyncio.Lock()
        self._output_lock = asyncio.Lock()
        self._play_task: asyncio.Task | None = None

    async def connect(self, socket):
        if self._socket is not None or self._closed.is_set():
            raise MediaError("媒体流不能重复连接")
        self._socket = socket
        try:
            raw = await asyncio.wait_for(socket.recv(), self.timeout)
            hello = json.loads(raw) if isinstance(raw, str) else {}
            if not isinstance(hello, dict):
                raise MediaError("MEDIA_START 必须为 JSON 对象")
            size, ptime = hello.get("optimal_frame_size"), hello.get("ptime")
            if (
                hello.get("event") != "MEDIA_START"
                or hello.get("format") != MEDIA_FORMATS[self.sample_rate]
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or size % 2
                or size > 65500
                or not isinstance(ptime, (int, float))
                or ptime <= 0
                or abs(size / (2 * self.sample_rate) - ptime / 1000) > 0.001
            ):
                raise MediaError("MEDIA_START 格式不匹配；需要 JSON PCM 媒体协议")
            self._frame_size = size
            self._window = max(1, int(self._window_ms / ptime))
        except (ValueError, TypeError):
            raise MediaError("无效 MEDIA_START") from None
        self._reader = asyncio.create_task(self._read_loop(), name="grandstream-media-reader")

    def _check(self, generation: int | None = None):
        if self._error:
            raise self._error
        if self._closed.is_set():
            raise CallClosed("音频流已关闭")
        if self._socket is None or not self._frame_size:
            raise MediaError("请先 answer() 或等待外呼建立")
        if generation is not None and generation != self._generation:
            raise PlaybackInterrupted("播放已清除")

    def _finish(self, error: Exception | None = None):
        if not self._closed.is_set():
            self._error = error
            self._closed.set()
            self._changed.set()
            self._input_changed.set()

    async def _read_loop(self):
        try:
            async for raw in self._socket:
                if isinstance(raw, bytes):
                    if len(raw) % 2:
                        raise MediaError("收到不完整 PCM16 采样")
                    if not raw:
                        continue
                    if self._input_bytes + len(raw) > self._receive_limit:
                        raise AudioOverflow("音频接收积压超过容量")
                    self._input.append(raw)
                    self._input_bytes += len(raw)
                    self._input_changed.set()
                else:
                    event = json.loads(raw)
                    kind = event.get("event")
                    if kind == "MEDIA_XOFF":
                        self._xoff = True
                    elif kind == "MEDIA_XON":
                        self._xoff = False
                    elif kind == "MEDIA_MARK_PROCESSED":
                        self._marks.pop(event.get("correlation_id"), None)
                    elif kind == "ERROR":
                        raise MediaError("Asterisk 媒体控制错误")
                    self._changed.set()
            self._finish(MediaError("媒体连接已断开"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._finish(exc if isinstance(exc, MediaError) else MediaError("媒体连接或协议错误"))

    async def receive(self, sample_rate: int | None = None):
        if self._receiving:
            raise MediaError("音频流只允许一个接收者")
        target = self.sample_rate if sample_rate is None else validate_rate(sample_rate)
        resampler = Resampler(self.sample_rate, target)
        self._receiving = True
        try:
            while True:
                if self._error:
                    raise self._error
                if self._input:
                    raw = self._input.popleft()
                    self._input_bytes -= len(raw)
                    data = resampler.feed(raw)
                    if data:
                        yield AudioFrame(data, target)
                elif self._closed.is_set():
                    break
                else:
                    self._input_changed.clear()
                    await self._input_changed.wait()
            tail = resampler.feed(b"", last=True)
            if tail:
                yield AudioFrame(tail, target)
        finally:
            self._receiving = False

    def __aiter__(self):
        return self.receive()

    async def _wait(self, predicate, generation):
        async def wait_ready():
            while True:
                self._check(generation)
                if predicate():
                    return
                self._changed.clear()
                await self._changed.wait()

        try:
            await asyncio.wait_for(wait_ready(), self.timeout)
        except asyncio.TimeoutError:
            error = MediaError("等待 Asterisk 媒体流控或播放确认超时")
            self._finish(error)
            raise error from None

    async def _send(self, value):
        try:
            await asyncio.wait_for(self._socket.send(value), self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            error = MediaError("媒体发送失败")
            self._finish(error)
            raise error from None

    async def _send_frames(self, generation):
        while len(self._wire) >= self._frame_size:
            await self._wait(lambda: not self._xoff and len(self._marks) < self._window, generation)
            async with self._send_lock:
                self._check(generation)
                payload = bytes(self._wire[: self._frame_size])
                del self._wire[: self._frame_size]
                self._sequence += 1
                mark = f"{generation}-{self._sequence}"
                self._marks[mark] = None
                await self._send(payload)
                await self._send(json.dumps({"command": "MARK_MEDIA", "correlation_id": mark}))

    async def write(self, frame: AudioFrame):
        if self._play_task is not None and self._play_task is not asyncio.current_task():
            raise MediaError("play() 正在持有输出流")
        generation = self._generation
        async with self._output_lock:
            self._check(generation)
            if self._resampler is None:
                self._resampler = Resampler(frame.sample_rate, self.sample_rate)
            if self._resampler.source != frame.sample_rate:
                raise ValueError("连续输出流不能改变采样率；请先 drain() 或 clear()")
            # Even a large caller-owned chunk is converted and queued in bounded slices.
            size = max(2, frame.sample_rate // 50 * 2)
            for offset in range(0, len(frame.pcm), size):
                self._check(generation)
                self._wire.extend(self._resampler.feed(frame.pcm[offset : offset + size]))
                await self._send_frames(generation)

    async def drain(self):
        if self._play_task is not None and self._play_task is not asyncio.current_task():
            raise MediaError("play() 正在持有输出流")
        generation = self._generation
        async with self._output_lock:
            self._check(generation)
            if self._resampler:
                self._wire.extend(self._resampler.feed(b"", last=True))
                self._resampler = None
            if self._wire:
                self._wire.extend(bytes((-len(self._wire)) % self._frame_size))
                await self._send_frames(generation)
            await self._wait(lambda: not self._marks, generation)

    async def play(self, source: AsyncIterable[AudioFrame]):
        if self._play_task is not None:
            raise MediaError("已有播放任务；先 clear() 再开始下一次播放")
        self._check()
        iterator = aiter(source)
        self._play_task = asyncio.current_task()
        generation = self._generation
        try:
            async for frame in iterator:
                self._check(generation)
                await self.write(frame)
            await self.drain()
        except BaseException:
            if not self._closed.is_set() and generation == self._generation:
                await self.clear()
            raise
        finally:
            try:
                if hasattr(iterator, "aclose"):
                    await iterator.aclose()
            finally:
                self._play_task = None

    async def clear(self):
        task = self._play_task
        async with self._send_lock:
            self._generation += 1
            self._wire.clear()
            self._resampler = None
            self._marks.clear()
            self._xoff = False
            self._changed.set()
            if self._socket and not self._closed.is_set():
                await self._send(json.dumps({"command": "FLUSH_MEDIA"}))
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def wait_closed(self):
        await self._closed.wait()
        if self._error:
            raise self._error

    async def close(self):
        self._finish()
        if self._reader and self._reader is not asyncio.current_task():
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        task = self._play_task
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._socket:
            await self._socket.close()
        self._marks.clear()
        self._wire.clear()
        self._resampler = None
