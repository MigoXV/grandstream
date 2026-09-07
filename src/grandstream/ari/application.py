"""ARI application lifecycle and asynchronous call dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import deque
from collections.abc import Awaitable, Callable, Sequence

from grandstream.audio.pcm import MEDIA_FORMATS
from grandstream.errors import ConnectionLost, DialFailed, GrandstreamError
from grandstream.models import HT813, Port

from .call import Call
from .client import AriClient, AriConfig

logger = logging.getLogger(__name__)
CallHandler = Callable[[Call], Awaitable[None]]


class AriApplication:
    def __init__(
        self,
        name: str,
        *,
        ari: AriConfig | AriClient,
        devices: Sequence[HT813] = (),
        media_sample_rate: int = 8000,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("ARI 应用名只能包含字母、数字、下划线和连字符")
        if media_sample_rate not in MEDIA_FORMATS:
            raise ValueError("不支持的媒体连接采样率")
        self.name, self.media_sample_rate = name, media_sample_rate
        self.ari = ari if isinstance(ari, AriClient) else AriClient(ari)
        self.devices = tuple(devices)
        self._endpoints = {}
        for device in devices:
            for port in device.ports:
                name = device.endpoint_name(port)
                if name in self._endpoints:
                    raise ValueError("endpoint 名称重复")
                self._endpoints[name] = (device, port)
        self._calls: dict[str, Call] = {}
        self._internal: set[str] = set()
        self._retired: set[str] = set()
        self._retirement_order: deque[str] = deque()
        self._handlers: list[tuple[str | None, CallHandler]] = []
        self._dtmf: dict[str, CallHandler] = {}
        self._listener = None
        self._shutdown = None
        self._ready = asyncio.Event()
        self._stopping = False
        self.connected = False
        self.error: GrandstreamError | None = None
        self._connection_handlers: list[Callable[[bool, GrandstreamError | None], None]] = []
        self._caller_handlers: list[Callable[[Call], None]] = []

    @property
    def calls(self) -> tuple[Call, ...]:
        return tuple(self._calls.values())

    def resolve_endpoint(self, endpoint):
        return self._endpoints.get(endpoint, (None, None))

    def _retire(self, *ids: str):
        # Late/duplicate StasisStart events must not recreate a completed call.
        for cid in ids:
            if cid not in self._retired:
                self._retired.add(cid)
                self._retirement_order.append(cid)
        while len(self._retirement_order) > 4096:
            self._retired.discard(self._retirement_order.popleft())

    def incoming_call(self, function: CallHandler | None = None, *, from_port: str | None = None):
        if from_port not in (None, "fxs", "fxo"):
            raise ValueError("from_port 必须为 fxs 或 fxo")

        def register(handler):
            self._handlers.append((from_port, handler))
            return handler

        return register(function) if function else register

    def connection_changed(self, handler: Callable[[bool, GrandstreamError | None], None]):
        """Register a synchronous, nonblocking connection status observer."""
        self._connection_handlers.append(handler)
        return handler

    def caller_changed(self, handler: Callable[[Call], None]):
        """Register a synchronous observer of an existing call's changed number."""
        self._caller_handlers.append(handler)
        return handler

    @staticmethod
    def _notify(handlers, *args):
        for handler in handlers:
            try:
                handler(*args)
            except Exception:
                logger.warning("SDK 状态通知回调失败", exc_info=True)

    def _connection(self, connected, error=None):
        self.connected, self.error = connected, error
        self._notify(self._connection_handlers, connected, error)

    def dtmf(self, digit: str):
        if not re.fullmatch(r"[0-9A-D*#]", digit):
            raise ValueError("无效 DTMF 按键")

        def register(handler):
            self._dtmf[digit] = handler
            return handler

        return register

    async def _handle(self, call, handler, *, finish=False):
        try:
            await handler(call)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            call.error = exc if isinstance(exc, GrandstreamError) else GrandstreamError("通话处理器异常")
            logger.warning("通话处理器失败 call=%s error=%s", call.id, type(exc).__name__)
            finish = True
        finally:
            if finish and not call.closed:
                await call.hangup()

    def _endpoint(self, event):
        args = event.get("args", [])
        if args and args[0] in self._endpoints:
            return args[0]
        name = event.get("channel", {}).get("name", "")
        if name.startswith("PJSIP/"):
            return name[6:].rsplit("-", 1)[0]
        return None

    async def _dispatch(self, event: dict):
        kind = event.get("type")
        channel = event.get("channel", {})
        cid = channel.get("id")
        if not cid or cid in self._internal or cid in self._retired:
            return
        call = self._calls.get(cid)
        if call and call.closed:
            return
        if kind == "StasisStart":
            if call:
                call.update(channel)
                call._entered.set()
                return
            call = Call(self, channel, endpoint=self._endpoint(event))
            self._calls[cid] = call
            call._entered.set()
            handler = next(
                (
                    handler
                    for port, handler in self._handlers
                    if port is None or (call.port and call.port.type == port)
                ),
                None,
            )
            if handler:
                call._spawn(self._handle(call, handler, finish=True))
            else:
                call._spawn(call.hangup())
        elif call:
            if kind in {"ChannelCallerId", "ChannelStateChange"}:
                previous = call.caller
                call.update(channel)
                if call.caller != previous:
                    self._notify(self._caller_handlers, call)
            elif kind == "ChannelDtmfReceived" and event.get("digit") in self._dtmf:
                if len(call._tasks) >= 32:
                    call.error = GrandstreamError("DTMF 处理器积压超过容量")
                    call._spawn(call.hangup())
                else:
                    call._spawn(self._handle(call, self._dtmf[event["digit"]]))
            elif kind in {"StasisEnd", "ChannelDestroyed"}:
                if call.outbound and not call._entered.is_set():
                    call.error = DialFailed(
                        f"外呼失败，挂机原因 {event.get('cause', 'unknown')}", cause=event.get("cause")
                    )
                call._spawn(call.hangup())

    async def _listen(self):
        delay = 1
        while not self._stopping:
            socket = None
            error = None
            try:
                socket = await self.ari.events(self.name)
                self._connection(True)
                self._ready.set()
                delay = 1
                async for raw in socket:
                    event = json.loads(raw)
                    if event.get("type") == "ApplicationReplaced":
                        # Reconnecting here would steal the application back indefinitely.
                        self._stopping = True
                        raise ConnectionLost("ARI 应用被另一个客户端接管")
                    await self._dispatch(event)
                error = ConnectionLost("ARI 事件连接已断开")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = exc if isinstance(exc, GrandstreamError) else ConnectionLost("ARI 事件连接失败")
                logger.warning("ARI 事件连接中断 error=%s", type(exc).__name__)
            finally:
                self._connection(False, error)
                self._ready.clear()
                if socket:
                    await socket.close()
                if error is not None:
                    for call in self.calls:
                        call.error = call.error or error
                await asyncio.gather(*(call.hangup() for call in self.calls), return_exceptions=True)
            if not self._stopping:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def start(self, *, wait_connected: bool = True):
        if self._stopping:
            raise ConnectionLost("应用已关闭；请创建新的 AriApplication")
        if self._listener is None:
            self._listener = asyncio.create_task(self._listen(), name="grandstream-ari-events")
        if not wait_connected:
            return
        try:
            await asyncio.wait_for(self._ready.wait(), self.ari.config.timeout)
        except BaseException:
            await self.close()
            raise

    async def dial(self, port: Port, *, number: str | None = None, timeout: float = 30) -> Call:
        if not self.connected:
            raise ConnectionLost("请先启动 ARI 应用")
        item = next(
            ((name, device) for name, (device, candidate) in self._endpoints.items() if candidate is port),
            None,
        )
        if item is None:
            raise ValueError("端口未注册到应用")
        if timeout <= 0:
            raise ValueError("外呼超时必须为正数")
        endpoint, device = item
        if port.type == "fxo":
            if not number or not re.fullmatch(r"\+?[0-9*#]+", number):
                raise ValueError("FXO 外呼需要有效号码")
            if not device.fxo.outbound or device.fxo.outbound.stage_method != 1:
                raise ValueError("FXO 外呼需要明确配置 stage_method=1")
        elif number is not None:
            raise ValueError("呼叫 FXS 电话不需要 number")
        if any(call.port is port and not call.closed for call in self.calls):
            raise DialFailed("端口正被通话占用", reason="busy")
        cid = "gs-call-" + uuid.uuid4().hex
        call = Call(self, {"id": cid, "state": "Down"}, endpoint=endpoint, outbound=True)
        self._calls[cid] = call
        try:
            await self.ari.request(
                "POST",
                f"channels/{cid}",
                params={
                    "endpoint": f"PJSIP/{number}@{endpoint}" if number else f"PJSIP/{endpoint}",
                    "app": self.name,
                    "appArgs": endpoint,
                    "timeout": timeout,
                },
            )
            await call._wait_entered(timeout)
            await call.answer()
            return call
        except BaseException:
            await call.hangup()
            raise

    async def run(self):
        async with self:
            await self._listener

    async def _stop(self):
        self._stopping = True
        if self._listener:
            self._listener.cancel()
            await asyncio.gather(self._listener, return_exceptions=True)
        await asyncio.gather(*(call.hangup() for call in self.calls), return_exceptions=True)
        await self.ari.close()

    async def close(self):
        if self._shutdown is None:
            self._shutdown = asyncio.create_task(self._stop())
        await asyncio.shield(self._shutdown)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *_):
        await self.close()
