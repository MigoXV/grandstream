"""PCM16LE frames and stateful, optional sample rate conversion."""

from dataclasses import dataclass

import numpy as np
import soxr

MEDIA_FORMATS = {
    8000: "slin",
    12000: "slin12",
    16000: "slin16",
    24000: "slin24",
    32000: "slin32",
    44100: "slin44",
    48000: "slin48",
    96000: "slin96",
    192000: "slin192",
}


def validate_rate(rate: int) -> int:
    if isinstance(rate, bool) or not isinstance(rate, int) or not 1000 <= rate <= 192000:
        raise ValueError("采样率必须为 1000 至 192000 的整数")
    return rate


@dataclass(frozen=True)
class AudioFrame:
    """One chunk of mono PCM16LE; chunk boundaries need not equal media frames."""

    pcm: bytes
    sample_rate: int

    def __post_init__(self):
        validate_rate(self.sample_rate)
        if not isinstance(self.pcm, bytes) or len(self.pcm) % 2:
            raise ValueError("音频必须为完整采样的 PCM16LE bytes")

    @property
    def duration(self) -> float:
        return len(self.pcm) / (2 * self.sample_rate)


class Resampler:
    def __init__(self, source: int, target: int):
        self.source, self.target = validate_rate(source), validate_rate(target)
        self._stream = (
            None if source == target else soxr.ResampleStream(source, target, 1, dtype="int16", quality="HQ")
        )
        self.ended = False

    def feed(self, pcm: bytes, *, last: bool = False) -> bytes:
        if self.ended:
            raise ValueError("重采样流已结束")
        if len(pcm) % 2:
            raise ValueError("PCM16 字节数必须为偶数")
        self.ended = last
        if self._stream is None:
            return pcm
        data = np.frombuffer(pcm, dtype="<i2").astype(np.int16, copy=False)
        return self._stream.resample_chunk(data, last=last).astype("<i2", copy=False).tobytes()
