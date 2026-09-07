"""通过独立 ARI 应用和两个媒体通道测试本机 Asterisk；不拨打 PSTN。"""

import argparse
import asyncio
import json
import uuid

import numpy as np

from grandstream import AriApplication, AriClient, AriConfig, AudioFrame, AudioStream


async def main(media_sample_rate=8000, receive_sample_rate=None):
    config = AriConfig.from_env()
    name = "grandstream-smoke-" + uuid.uuid4().hex[:12]
    application = AriApplication(name, ari=config, media_sample_rate=media_sample_rate)
    answered = asyncio.Event()

    @application.incoming_call
    async def echo(call):
        await call.answer()
        answered.set()
        async for frame in call.audio.receive(sample_rate=receive_sample_rate):
            await call.audio.write(frame)

    probe_id = "probe-" + uuid.uuid4().hex
    probe = AudioStream(receive_buffer_seconds=5)
    async with application, AriClient(config) as control:
        resources = set()
        try:
            await control.request(
                "POST",
                "channels/externalMedia",
                params={
                    "app": name,
                    "channelId": probe_id,
                    "external_host": "INCOMING",
                    "transport": "websocket",
                    "encapsulation": "none",
                    "connection_type": "server",
                    "format": "slin",
                    "direction": "both",
                    "transport_data": "f(json)",
                },
            )
            variable = await control.request(
                "GET", f"channels/{probe_id}/variable", params={"variable": "MEDIA_WEBSOCKET_CONNECTION_ID"}
            )
            await probe.connect(await control.media(variable["value"]))
            await asyncio.wait_for(answered.wait(), 10)
            resources = {call._media_id for call in application.calls}
            bridges = {call._bridge_id for call in application.calls}
            received = bytearray()
            enough = asyncio.Event()

            async def read():
                async for frame in probe:
                    received.extend(frame.pcm)
                    # Ignore initial bridge silence when deciding whether an echo arrived.
                    samples = np.frombuffer(received, dtype="<i2").astype(np.int32)
                    if np.count_nonzero(np.abs(samples) > 100) > 6000:
                        enough.set()

            reader = asyncio.create_task(read())
            try:
                source_rate = 24000
                t = np.arange(source_rate) / source_rate
                pcm = (np.sin(2 * np.pi * 440 * t) * 10000).astype("<i2").tobytes()
                await probe.write(AudioFrame(pcm, source_rate))
                await probe.drain()
                try:
                    await asyncio.wait_for(enough.wait(), 10)
                except asyncio.TimeoutError:
                    samples = np.frombuffer(received, dtype="<i2").astype(np.int32)
                    print(
                        json.dumps(
                            {
                                "received_samples": len(samples),
                                "nonzero_samples": int(np.count_nonzero(np.abs(samples) > 100)),
                                "peak": int(np.max(np.abs(samples))) if len(samples) else 0,
                                "reader_done": reader.done(),
                                "call_errors": [type(c.error).__name__ for c in application.calls],
                            }
                        )
                    )
                    raise
                print(
                    json.dumps(
                        {
                            "application": name,
                            "input_rate": source_rate,
                            "probe_media_rate": probe.sample_rate,
                            "application_media_rate": media_sample_rate,
                            "receive_sample_rate": receive_sample_rate,
                            "received_bytes": len(received),
                            "duplex_echo": "passed",
                        }
                    )
                )
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        finally:
            await application.close()
            await probe.close()
            # The application owns the probe call after StasisStart. It may already be gone.
            from grandstream.errors import AriError

            try:
                await control.request("DELETE", f"channels/{probe_id}")
            except AriError as exc:
                if exc.status != 404:
                    raise
            remaining = await control.request("GET", "channels")
            assert all(channel["id"] not in resources | {probe_id} for channel in remaining)
            remaining_bridges = await control.request("GET", "bridges")
            if resources:
                assert all(bridge["id"] not in bridges for bridge in remaining_bridges)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-sample-rate", type=int, default=8000)
    parser.add_argument("--receive-sample-rate", type=int)
    args = parser.parse_args()
    asyncio.run(main(args.media_sample_rate, args.receive_sample_rate))
