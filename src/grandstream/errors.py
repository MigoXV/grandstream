"""Public, credential-free SDK exceptions."""


class GrandstreamError(Exception):
    """Base error exposed by the SDK."""


class AriError(GrandstreamError):
    def __init__(self, status: int, operation: str):
        self.status = status
        super().__init__(f"ARI {operation}: HTTP {status}")


class ConnectionLost(GrandstreamError):
    pass


class CallClosed(GrandstreamError):
    pass


class DialFailed(GrandstreamError):
    pass


class MediaError(GrandstreamError):
    pass


class AudioOverflow(MediaError):
    pass


class PlaybackInterrupted(MediaError):
    pass
