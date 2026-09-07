"""A single application-owned call and its resources."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from urllib.parse import quote

from grandstream.audio import AudioStream
from grandstream.audio.pcm import MEDIA_FORMATS
from grandstream.errors import AriError, CallClosed, DialFailed, GrandstreamError

logger = logging.getLogger(__name__)


class Call:
    def __init__(self, application, channel: dict, *, endpoint: str | None = None, outbound: bool = False):
        self.application = application
        self.id = channel["id"]
        self.channel = channel
        self.endpoint = endpoint
        self.device, self.port = application.resolve_endpoint(endpoint)
        self.outbound = outbound
        self.audio = AudioStream(
            sample_rate=application.media_sample_rate, timeout=application.ari.config.timeout
        )
        self.error: Exception | None = None
        self.closed = False
        self._ended = asyncio.Event()
        self._entered = asyncio.Event()
        self._close_task = None
        self._setup_task = None
        self._tasks: set[asyncio.Task] = set()
        self._bridge_id = "gs-bridge-" + uuid.uuid4().hex
        self._media_id = "gs-media-" + uuid.uuid4().hex
        self._resources_started = False

    @property
    def caller(self) -> str:
        return self.channel.get("caller", {}).get("number", "")

    @property
    def state(self) -> str:
        return "closed" if self.closed else self.channel.get("state", "Down")

    def update(self, channel: dict):
        if not self.closed:
            self.channel.update(channel)

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _request(self, method, suffix="", **kwargs):
        if self.closed:
            raise CallClosed("通话已结束")
        return await self.application.ari.request(
            method, f"channels/{quote(self.id, safe='')}{suffix}", **kwargs
        )

    async def answer(self):
        if self.closed:
            raise CallClosed("通话已结束")
        if self._setup_task is None:
            self._setup_task = asyncio.create_task(self._setup(), name="grandstream-call-setup")
        try:
            await asyncio.shield(self._setup_task)
        except BaseException:
            await self.hangup()
            raise

    async def _setup(self):
        ari = self.application.ari
        self._resources_started = True
        self.application._internal.add(self._media_id)
        try:
            await ari.request(
                "POST", "bridges", params={"type": "mixing,proxy_media", "bridgeId": self._bridge_id}
            )
            await ari.request(
                "POST",
                "channels/externalMedia",
                params={
                    "app": self.application.name,
                    "channelId": self._media_id,
                    "external_host": "INCOMING",
                    "transport": "websocket",
                    "encapsulation": "none",
                    "connection_type": "server",
                    "format": MEDIA_FORMATS[self.audio.sample_rate],
                    "direction": "both",
                    "transport_data": "f(json)",
                },
            )
            variable = await ari.request(
                "GET",
                f"channels/{self._media_id}/variable",
                params={"variable": "MEDIA_WEBSOCKET_CONNECTION_ID"},
            )
            socket = await ari.media(variable["value"])
            await self.audio.connect(socket)
            await ari.request(
                "POST",
                f"bridges/{self._bridge_id}/addChannel",
                params={"channel": f"{self.id},{self._media_id}"},
            )
            if not self.outbound:
                await self._request("POST", "/answer")
            self._spawn(self._monitor_media())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc if isinstance(exc, GrandstreamError) else GrandstreamError("通话媒体初始化失败")
            raise self.error from None

    async def _monitor_media(self):
        try:
            await self.audio.wait_closed()
        except Exception as exc:
            self.error = exc
        finally:
            if not self.closed:
                await self.hangup()

    async def send_dtmf(self, digits: str, *, duration_ms: int = 100):
        if not re.fullmatch(r"[0-9A-D*#]+", digits) or not 40 <= duration_ms <= 5000:
            raise ValueError("需要有效 DTMF 字符和 40 至 5000 ms 持续时间")
        await self._request("POST", "/dtmf", params={"dtmf": digits, "between": 100, "duration": duration_ms})

    async def _wait_entered(self, timeout: float):
        entered = asyncio.create_task(self._entered.wait())
        ended = asyncio.create_task(self._ended.wait())
        try:
            done, _ = await asyncio.wait(
                (entered, ended), timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise DialFailed("外呼超时")
            if self.closed:
                raise self.error or DialFailed("外呼未接通")
        finally:
            entered.cancel()
            ended.cancel()
            await asyncio.gather(entered, ended, return_exceptions=True)

    async def hangup(self):
        initiated = self._close_task is None
        if initiated:
            self.closed = True
            origin = asyncio.current_task()
            self._close_task = asyncio.create_task(self._cleanup(origin), name="grandstream-call-cleanup")
        # Cleanup may be cancelling a handler which has its own finally: hangup().
        # That handler must not wait back on the cleanup task awaiting the handler.
        if not initiated and asyncio.current_task() in self._tasks:
            return
        await asyncio.shield(self._close_task)

    async def _cleanup(self, origin):
        try:
            tasks = [task for task in self._tasks if task is not origin]
            if self._setup_task and self._setup_task is not origin:
                tasks.append(self._setup_task)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await self.audio.close()
            except Exception:
                logger.warning("关闭媒体连接失败 call=%s", self.id)
            resources = []
            if self._resources_started:
                resources.extend((f"channels/{self._media_id}", f"bridges/{self._bridge_id}"))
            resources.append(f"channels/{quote(self.id, safe='')}")
            for path in resources:
                try:
                    await self.application.ari.request("DELETE", path)
                except AriError as exc:
                    if exc.status != 404:
                        logger.warning("ARI 资源清理失败 status=%s call=%s", exc.status, self.id)
                except Exception:
                    logger.warning("ARI 资源清理时连接不可用 call=%s", self.id)
        finally:
            self.application._retire(self.id, self._media_id)
            self.application._calls.pop(self.id, None)
            self.application._internal.discard(self._media_id)
            self._ended.set()

    async def wait_closed(self):
        await self._ended.wait()
        if self.error:
            raise self.error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.hangup()
