"""Small ARI REST client and authenticated WebSocket connections."""

import base64
import os
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from pydantic import Field, SecretStr, field_validator
from websockets.asyncio.client import connect

from grandstream.errors import AriError, ConnectionLost
from grandstream.models.config import ConfigModel


class AriConfig(ConfigModel):
    url: str = "http://127.0.0.1:8088/ari"
    username: str = Field(min_length=1)
    password: SecretStr
    media_url: str | None = None
    timeout: float = Field(default=10, gt=0)

    @field_validator("url", "media_url")
    @classmethod
    def valid_url(cls, value):
        if value is None:
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https", "ws", "wss"} or not parts.hostname:
            raise ValueError("需要完整的 HTTP 或 WebSocket 地址")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("地址不能包含凭据、查询参数或片段")
        return value.rstrip("/")

    @classmethod
    def from_env(cls):
        password = os.environ.get("GRANDSTREAM_ARI_PASSWORD")
        if not password:
            raise ValueError("请设置 GRANDSTREAM_ARI_PASSWORD")
        return cls(
            url=os.getenv("GRANDSTREAM_ARI_URL", "http://127.0.0.1:8088/ari"),
            username=os.getenv("GRANDSTREAM_ARI_USERNAME", "grandstream"),
            password=password,
            media_url=os.getenv("GRANDSTREAM_MEDIA_URL"),
        )


def websocket_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (
            {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme),
            parts.netloc,
            parts.path,
            parts.query,
            "",
        )
    )


class AriClient:
    def __init__(self, config: AriConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        if urlsplit(config.url).scheme not in {"http", "https"}:
            raise ValueError("ARI REST url 必须为 http 或 https")
        self.config = config
        token = base64.b64encode(f"{config.username}:{config.password.get_secret_value()}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self._http = httpx.AsyncClient(
            base_url=config.url + "/",
            headers=self._headers,
            timeout=config.timeout,
            trust_env=False,
            transport=transport,
        )

    async def request(self, method: str, path: str, **kwargs):
        try:
            response = await self._http.request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError:
            raise ConnectionLost("ARI HTTP 连接失败") from None
        if response.is_error:
            # Do not include the URL, response body, channel variables, or credentials.
            raise AriError(response.status_code, method)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ConnectionLost("ARI 未返回有效 JSON") from None

    async def events(self, app: str):
        url = websocket_url(self.config.url) + "/events?" + urlencode({"app": app})
        return await connect(
            url,
            additional_headers=self._headers,
            proxy=None,
            open_timeout=self.config.timeout,
            close_timeout=2,
            max_size=1024 * 1024,
        )

    async def media(self, connection_id: str):
        from urllib.parse import quote

        base = self.config.media_url
        if base is None:
            if not self.config.url.endswith("/ari"):
                raise ValueError("非标准 ARI 路径需要显式配置 media_url")
            base = self.config.url[:-4] + "/media"
        return await connect(
            websocket_url(base) + "/" + quote(connection_id, safe=""),
            subprotocols=["media"],
            additional_headers=self._headers,
            proxy=None,
            open_timeout=self.config.timeout,
            close_timeout=2,
            max_size=65500,
        )

    async def close(self):
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()
