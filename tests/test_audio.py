import asyncio
import json
import wave

import numpy as np
import pytest

from grandstream import AudioFrame, AudioStream, Resampler, WavSink, WavSource
from grandstream.errors import AudioOverflow, CallClosed, MediaError, PlaybackInterrupted

from .fakes import Socket, eventually


def signal(rate, seconds=0.1):
    t = np.arange(int(rate * seconds)) / rate
    return (np.sin(t * 2 * np.pi * 440) * 16000).astype("<i2").tobytes()


def assert_pcm_equal(actual, expected):
    # Independent libsoxr int16 streams apply quantization/dither independently.
    # A two-LSB tolerance preserves the signal and still detects discontinuities.
    np.testing.assert_allclose(
        np.frombuffer(actual, dtype="<i2").astype(float), np.frombuffer(expected, dtype="<i2"), rtol=0, atol=2
    )


def test_pcm_metadata_and_validation():
    assert AudioFrame(bytes(320), 8000).duration == 0.02
    for pcm, rate in ((b"x", 8000), (b"", 0), (b"", True)):
        with pytest.raises(ValueError):
            AudioFrame(pcm, rate)


@pytest.mark.parametrize("source,target", [(8000, 8000), (8000, 16000), (24000, 8000), (44100, 16000)])
def test_streaming_resampler_matches_single_stream(source, target):
    pcm = signal(source)
    expected = Resampler(source, target).feed(pcm, last=True)
    resampler = Resampler(source, target)
    actual = b"".join(resampler.feed(pcm[i : i + 94]) for i in range(0, len(pcm), 94))
    actual += resampler.feed(b"", last=True)
    assert_pcm_equal(actual, expected)
    assert abs(len(actual) // 2 - target // 10) <= 1
    if source == target:
        assert actual == pcm
    with pytest.raises(ValueError):
        resampler.feed(b"")


@pytest.mark.parametrize("target", [None, 16000])
async def test_receive_native_or_resampled_and_tail(target):
    socket = Socket(rate=8000)
    stream = AudioStream()
    await stream.connect(socket)
    pcm = signal(8000)
    socket.queue.put_nowait(pcm)
    await eventually(lambda: stream._input_bytes == len(pcm))
    await stream.close()
    frames = [frame async for frame in stream.receive(target)]
    assert {frame.sample_rate for frame in frames} == {target or 8000}
    assert_pcm_equal(b"".join(f.pcm for f in frames), Resampler(8000, target or 8000).feed(pcm, last=True))


async def test_write_resamples_pads_tail_and_drains():
    socket = Socket(rate=8000)
    stream = AudioStream()
    await stream.connect(socket)
    try:
        pcm = signal(24000, 0.135)
        for offset in range(0, len(pcm), 126):
            await stream.write(AudioFrame(pcm[offset : offset + 126], 24000))
        await stream.drain()
        sent = b"".join(item for item in socket.sent if isinstance(item, bytes))
        expected = Resampler(24000, 8000).feed(pcm, last=True)
        assert_pcm_equal(sent[: len(expected)], expected)
        assert not any(sent[len(expected) :])
        assert len(sent) % 320 == 0
        assert all(len(item) == 320 for item in socket.sent if isinstance(item, bytes))
        assert not stream._marks
        await stream.write(AudioFrame(bytes(320), 8000))
        await stream.drain()
    finally:
        await stream.close()


async def test_rate_cannot_change_mid_output():
    stream, socket = AudioStream(), Socket(rate=8000)
    await stream.connect(socket)
    try:
        await stream.write(AudioFrame(bytes(50), 8000))
        with pytest.raises(ValueError):
            await stream.write(AudioFrame(bytes(50), 24000))
        await stream.clear()
        await stream.write(AudioFrame(bytes(50), 24000))
    finally:
        await stream.close()


async def test_xoff_and_window_backpressure_then_clear_wakes_writer():
    stream, socket = AudioStream(playback_window_ms=40), Socket(rate=8000, ack=False)
    await stream.connect(socket)
    try:
        socket.queue.put_nowait(json.dumps({"event": "MEDIA_XOFF"}))
        await eventually(lambda: stream._xoff)
        writer = asyncio.create_task(stream.write(AudioFrame(bytes(320 * 5), 8000)))
        await asyncio.sleep(0.01)
        assert not socket.sent
        socket.queue.put_nowait(json.dumps({"event": "MEDIA_XON"}))
        await eventually(lambda: len(stream._marks) == 2)
        assert len([x for x in socket.sent if isinstance(x, bytes)]) == 2
        await stream.clear()
        with pytest.raises(PlaybackInterrupted):
            await writer
    finally:
        await stream.close()


async def test_late_marks_after_clear_do_not_ack_new_audio():
    stream, socket = AudioStream(timeout=0.05), Socket(rate=8000, ack=False)
    await stream.connect(socket)
    try:
        await stream.write(AudioFrame(bytes(320), 8000))
        old = next(iter(stream._marks))
        await stream.clear()
        await stream.write(AudioFrame(bytes(320), 8000))
        new = next(iter(stream._marks))
        socket.queue.put_nowait(json.dumps({"event": "MEDIA_MARK_PROCESSED", "correlation_id": old}))
        await asyncio.sleep(0.01)
        assert new in stream._marks
        with pytest.raises(MediaError, match="超时"):
            await stream.drain()
    finally:
        await stream.close()


async def test_clear_cancels_play_without_closing_receive():
    stream, socket = AudioStream(), Socket(rate=8000)
    await stream.connect(socket)
    waiting = asyncio.Event()

    async def source():
        yield AudioFrame(bytes(320), 8000)
        waiting.set()
        await asyncio.Event().wait()

    try:
        player = asyncio.create_task(stream.play(source()))
        await waiting.wait()
        await asyncio.wait_for(stream.clear(), 1)
        assert player.cancelled()
        assert any(isinstance(x, str) and json.loads(x)["command"] == "FLUSH_MEDIA" for x in socket.sent)
        iterator = stream.receive()
        socket.queue.put_nowait(bytes(320))
        assert (await anext(iterator)).sample_rate == 8000
        await iterator.aclose()
    finally:
        await stream.close()


async def test_receive_overflow_closes_stream():
    stream, socket = AudioStream(receive_buffer_seconds=0.04), Socket(rate=8000)
    await stream.connect(socket)
    try:
        socket.queue.put_nowait(bytes(960))
        with pytest.raises(AudioOverflow):
            await stream.wait_closed()
        with pytest.raises(AudioOverflow):
            await anext(stream.receive())
    finally:
        await stream.close()


async def test_close_wakes_receiver_and_blocked_sender():
    stream, socket = AudioStream(playback_window_ms=20), Socket(rate=8000, ack=False)
    await stream.connect(socket)
    iterator = stream.receive()
    reader = asyncio.create_task(anext(iterator))
    writer = asyncio.create_task(stream.write(AudioFrame(bytes(960), 8000)))
    await eventually(lambda: len(stream._marks) == 1)
    await stream.close()
    with pytest.raises(CallClosed):
        await asyncio.wait_for(writer, 1)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(reader, 1)


async def test_receive_only_one_consumer():
    stream, socket = AudioStream(), Socket(rate=8000)
    await stream.connect(socket)
    first = asyncio.create_task(anext(stream.receive()))
    await eventually(lambda: stream._receiving)
    with pytest.raises(MediaError, match="一个接收者"):
        await anext(stream.receive())
    await stream.close()
    with pytest.raises(StopAsyncIteration):
        await first


async def test_wav_roundtrip_preserves_metadata(tmp_path):
    pcm = signal(24000)
    file = tmp_path / "audio.wav"
    async with WavSink(file) as sink:
        await sink.write(AudioFrame(pcm, 24000))
    frames = [frame async for frame in WavSource(file)]
    assert {f.sample_rate for f in frames} == {24000}
    assert b"".join(f.pcm for f in frames) == pcm
    with wave.open(str(file), "wb") as wav:
        wav.setparams((2, 2, 8000, 0, "NONE", "not compressed"))
        wav.writeframes(bytes(320))
    with pytest.raises(ValueError, match="单声道"):
        await anext(WavSource(file).__aiter__())


@pytest.mark.parametrize("hello", [b"binary", "not-json", '{"event":"MEDIA_START","format":"ulaw"}'])
async def test_invalid_handshake(hello):
    stream, socket = AudioStream(), Socket()
    socket.queue.put_nowait(hello)
    try:
        with pytest.raises(MediaError):
            await stream.connect(socket)
    finally:
        await stream.close()
