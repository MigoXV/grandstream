import asyncio
import json

from grandstream.ari import AriClient, AriConfig
from grandstream.audio.pcm import MEDIA_FORMATS
from grandstream.errors import AriError


class Socket:
    def __init__(self, *, rate=None, ack=True):
        self.queue = asyncio.Queue()
        self.sent = []
        self.ack = ack
        self.closed = False
        if rate:
            self.queue.put_nowait(
                json.dumps(
                    {
                        "event": "MEDIA_START",
                        "format": MEDIA_FORMATS[rate],
                        "optimal_frame_size": rate // 50 * 2,
                        "ptime": 20,
                    }
                )
            )

    async def recv(self):
        return await self.queue.get()

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.recv()
        if item is None:
            raise StopAsyncIteration
        return item

    async def send(self, value):
        self.sent.append(value)
        if isinstance(value, str) and self.ack:
            message = json.loads(value)
            if message.get("command") == "MARK_MEDIA":
                self.queue.put_nowait(
                    json.dumps({"event": "MEDIA_MARK_PROCESSED", "correlation_id": message["correlation_id"]})
                )

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)


class FakeAri(AriClient):
    def __init__(self):
        self.config = AriConfig(username="test", password="secret", timeout=0.5)
        self.requests = []
        self.events_socket = Socket()
        self.media_sockets = []
        self.resources = set()
        self.hook = None
        self.fail = None
        self.rate = 8000
        self.closed = False

    async def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        if self.hook:
            await self.hook(method, path, kwargs)
        if self.fail == (method, path):
            raise AriError(500, method)
        params = kwargs.get("params", {})
        if method == "POST" and path == "bridges":
            self.resources.add("bridges/" + params["bridgeId"])
        elif method == "POST" and path == "channels/externalMedia":
            self.rate = next(k for k, v in MEDIA_FORMATS.items() if v == params["format"])
            self.resources.add("channels/" + params["channelId"])
        elif method == "DELETE":
            self.resources.discard(path)
        if path.endswith("/variable"):
            return {"value": "connection-id"}
        return {}

    async def events(self, app):
        return self.events_socket

    async def media(self, connection_id):
        socket = Socket(rate=self.rate)
        self.media_sockets.append(socket)
        return socket

    async def close(self):
        self.closed = True


async def eventually(predicate, timeout=1):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(wait(), timeout)
