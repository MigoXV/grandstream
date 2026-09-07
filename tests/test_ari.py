import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from grandstream import HT813, AriApplication, AriClient, AriConfig, FxoOutbound, FxoPort
from grandstream.errors import AriError, AudioOverflow, ConnectionLost, DialFailed

from .fakes import FakeAri, eventually


@pytest_asyncio.fixture
async def runtime():
    ari = FakeAri()
    device = HT813(mac="000b82123456", fxo=FxoPort(endpoint="fxo1", outbound=FxoOutbound(stage_method=1)))
    app = AriApplication("unit-test", ari=ari, devices=[device])
    await app.start()
    yield app, ari, device
    await asyncio.wait_for(app.close(), 2)
    assert not ari.resources


async def incoming_call(app, *, cid="incoming", name="PJSIP/fxo1-000001"):
    await app._dispatch(
        {
            "type": "StasisStart",
            "channel": {"id": cid, "name": name, "caller": {"number": "123"}, "state": "Ring"},
            "args": ["fxo1"],
        }
    )
    return app._calls[cid]


async def test_incoming_dtmf_caller_update_internal_filter_and_cleanup(runtime):
    app, ari, device = runtime
    answered, dtmf = asyncio.Event(), asyncio.Event()

    @app.incoming_call(from_port="fxo")
    async def incoming(call):
        await call.answer()
        answered.set()
        async for _ in call.audio:
            pass

    @app.dtmf("1")
    async def on_one(call):
        await call.send_dtmf("2#")
        dtmf.set()

    call = await incoming_call(app)
    await answered.wait()
    assert call.device is device and call.port is device.fxo
    assert call.caller == "123"
    await app._dispatch({"type": "StasisStart", "channel": {"id": call._media_id}})
    assert len(app.calls) == 1
    await app._dispatch({"type": "ChannelCallerId", "channel": {"id": call.id, "caller": {"number": "456"}}})
    assert call.caller == "456"
    await app._dispatch({"type": "ChannelDtmfReceived", "channel": {"id": call.id}, "digit": "1"})
    await dtmf.wait()
    await app._dispatch({"type": "StasisEnd", "channel": {"id": call.id}})
    await app._dispatch({"type": "ChannelDestroyed", "channel": {"id": call.id}})
    await asyncio.wait_for(call.wait_closed(), 1)
    assert not app.calls and not ari.resources
    deletes = [path for method, path, _ in ari.requests if method == "DELETE"]
    assert len(deletes) == len(set(deletes)) == 3
    assert ari.media_sockets[0].closed
    for cid in (call.id, call._media_id):
        await app._dispatch({"type": "StasisStart", "channel": {"id": cid}})
    assert not app.calls


async def test_unknown_endpoint_is_a_normal_call(runtime):
    app, _, _ = runtime
    observed = []

    @app.incoming_call
    async def handler(call):
        observed.append((call.endpoint, call.device, call.port))

    await app._dispatch({"type": "StasisStart", "channel": {"id": "unknown", "name": "PJSIP/alice-0001"}})
    await eventually(lambda: observed)
    assert observed == [("alice", None, None)]


async def test_outbound_event_before_rest_response_does_not_dispatch_incoming(runtime):
    app, ari, device = runtime
    count = []

    @app.incoming_call
    async def wrong(call):
        count.append(call.id)

    async def hook(method, path, kwargs):
        if method == "POST" and path.startswith("channels/gs-call-"):
            await app._dispatch({"type": "StasisStart", "channel": {"id": path.split("/")[1], "state": "Up"}})

    ari.hook = hook
    call = await app.dial(device.fxo, number="12345")
    assert call.outbound and call.state == "Up" and not count
    assert any(data.get("params", {}).get("endpoint") == "PJSIP/12345@fxo1" for _, _, data in ari.requests)
    with pytest.raises(DialFailed, match="占用"):
        await app.dial(device.fxo, number="23456")
    await call.hangup()


@pytest.mark.parametrize("cause", [17, 18, 34])
async def test_outbound_failure_before_stasis(runtime, cause):
    app, ari, device = runtime

    async def hook(method, path, kwargs):
        if method == "POST" and path.startswith("channels/gs-call-"):
            await app._dispatch(
                {"type": "ChannelDestroyed", "channel": {"id": path.split("/")[1]}, "cause": cause}
            )

    ari.hook = hook
    with pytest.raises(DialFailed, match=str(cause)) as error:
        await app.dial(device.fxo, number="123")
    assert error.value.cause == cause
    assert error.value.reason == {17: "busy", 18: "no_answer"}.get(cause, "failed")
    assert not app.calls


async def test_outbound_timeout_and_cancellation_cleanup(runtime):
    app, _, device = runtime
    with pytest.raises(DialFailed, match="超时"):
        await app.dial(device.fxo, number="123", timeout=0.01)
    assert not app.calls
    task = asyncio.create_task(app.dial(device.fxo, number="123"))
    await eventually(lambda: app.calls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert not app.calls


async def test_failed_media_creation_cleans_partial_bridge(runtime):
    app, ari, _ = runtime
    ari.fail = ("POST", "channels/externalMedia")

    @app.incoming_call
    async def handler(call):
        await call.answer()

    call = await incoming_call(app)
    with pytest.raises(AriError):
        await asyncio.wait_for(call.wait_closed(), 1)
    assert not ari.resources and not app.calls


async def test_hangup_during_media_setup_cannot_leak_later_resources(runtime):
    app, ari, _ = runtime
    creating = asyncio.Event()

    async def hook(method, path, kwargs):
        if (method, path) == ("POST", "channels/externalMedia"):
            creating.set()
            await asyncio.Event().wait()

    ari.hook = hook

    @app.incoming_call
    async def handler(call):
        try:
            await call.answer()
        finally:
            await call.hangup()

    call = await incoming_call(app)
    await creating.wait()
    await asyncio.wait_for(call.hangup(), 1)
    assert not app.calls and not ari.resources


async def test_receive_overflow_ends_call_even_when_handler_not_reading(runtime):
    app, ari, _ = runtime
    ready = asyncio.Event()

    @app.incoming_call
    async def handler(call):
        await call.answer()
        ready.set()
        await asyncio.Event().wait()

    call = await incoming_call(app)
    await ready.wait()
    ari.media_sockets[0].queue.put_nowait(bytes(32002))
    with pytest.raises(AudioOverflow):
        await asyncio.wait_for(call.wait_closed(), 1)
    assert not ari.resources


async def test_event_disconnect_releases_calls(runtime):
    app, ari, _ = runtime
    ready = asyncio.Event()

    @app.incoming_call
    async def handler(call):
        await call.answer()
        ready.set()
        await asyncio.Event().wait()

    call = await incoming_call(app)
    await ready.wait()
    await ari.events_socket.close()
    with pytest.raises(ConnectionLost):
        await asyncio.wait_for(call.wait_closed(), 1)
    assert not ari.resources and not app.connected


async def test_application_replacement_does_not_steal_subscription_back(runtime):
    app, ari, _ = runtime
    ari.events_socket.queue.put_nowait(json.dumps({"type": "ApplicationReplaced"}))
    await eventually(lambda: app._listener.done())
    assert app._stopping and not app.connected


async def test_rest_path_and_credentials_not_in_error():
    captured = []

    async def handler(request):
        captured.append(request)
        return httpx.Response(401, text="secret")

    config = AriConfig(url="http://host/prefix/ari", username="test", password="secret")
    async with AriClient(config, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AriError) as error:
            await client.request("POST", "/channels")
    assert captured[0].url.path == "/prefix/ari/channels"
    assert captured[0].headers["authorization"].startswith("Basic ")
    assert "secret" not in str(error.value) and "http" not in str(error.value)


def test_configuration_rejects_credentials_in_url():
    with pytest.raises(ValueError):
        AriConfig(url="http://user:secret@host/ari", username="test", password="secret")


async def test_public_status_and_caller_observers(runtime):
    app, ari, _ = runtime
    numbers, statuses = [], []
    app.caller_changed(lambda call: numbers.append((call.id, call.caller)))
    app.connection_changed(lambda connected, error: statuses.append((connected, error)))

    @app.incoming_call
    async def handler(call):
        await asyncio.Event().wait()

    call = await incoming_call(app)
    event = {"type": "ChannelCallerId", "channel": {"id": call.id, "caller": {"number": "456"}}}
    await app._dispatch(event)
    await app._dispatch(event)
    assert numbers == [(call.id, "456")]
    await ari.events_socket.close()
    await eventually(lambda: statuses)
    assert statuses[0][0] is False and isinstance(statuses[0][1], ConnectionLost)
    assert app.error is statuses[0][1]


async def test_background_start_retries_initial_failure(monkeypatch):
    ari = FakeAri()
    app = AriApplication("retry-test", ari=ari)
    statuses = []
    app.connection_changed(lambda connected, error: statuses.append((connected, error)))
    attempts = 0

    async def events(name):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("private connection data")
        return ari.events_socket

    monkeypatch.setattr(ari, "events", events)
    try:
        await app.start(wait_connected=False)
        await eventually(lambda: len(statuses) >= 2, timeout=2)
        assert statuses[0][0] is False and isinstance(statuses[0][1], ConnectionLost)
        assert statuses[1] == (True, None)
        assert app.connected and app.error is None
    finally:
        await app.close()


async def test_observer_failure_does_not_interrupt_other_observers(runtime):
    app, _, _ = runtime
    received = []

    def fail(*args):
        raise ValueError("observer error")

    app.connection_changed(fail)
    app.connection_changed(lambda *args: received.append(args))
    await app.close()
    assert received == [(False, None)]


async def test_normal_shutdown_preserves_clean_call_status(runtime):
    app, _, _ = runtime
    finished = asyncio.Event()

    @app.incoming_call
    async def handler(call):
        try:
            await asyncio.Event().wait()
        finally:
            # Business cleanup may await local work without hanging up again.
            await asyncio.sleep(0)
            finished.set()

    call = await incoming_call(app)
    await asyncio.sleep(0)
    await asyncio.wait_for(app.close(), 1)
    assert finished.is_set() and call.closed and call.error is None


@pytest.mark.parametrize("cause,reason", [(17, "busy"), (18, "no_answer"), (19, "no_answer"), (34, "failed")])
def test_dial_failure_has_structured_reason(cause, reason):
    error = DialFailed("外呼失败", cause=cause)
    assert error.cause == cause and error.reason == reason
    assert DialFailed("外呼超时", reason="timeout").reason == "timeout"
