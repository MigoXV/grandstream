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
    """Structured outbound failure; cause is the Asterisk hangup cause, if supplied."""

    def __init__(self, message: str, *, reason: str | None = None, cause: int | None = None):
        self.cause = cause
        self.reason = reason or {17: "busy", 18: "no_answer", 19: "no_answer"}.get(cause, "failed")
        super().__init__(message)


class MediaError(GrandstreamError):
    pass


class AudioOverflow(MediaError):
    pass


class PlaybackInterrupted(MediaError):
    pass
