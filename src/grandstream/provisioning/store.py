"""Atomic publication; publishing does not imply the device has applied a file."""

import os
import tempfile
from pathlib import Path

from grandstream.models import HT813

from .xml import configuration_xml


class ProvisioningStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()

    def _publish(self, name: str, content: bytes) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            path = self.directory / name
            os.replace(temporary, path)
            return path
        finally:
            Path(temporary).unlink(missing_ok=True)

    def publish(self, device: HT813) -> Path:
        return self._publish(f"cfg{device.mac}.xml", device.render_xml())

    def publish_defaults(self, values: dict[str, str], *, model: str | None = None) -> Path:
        if model not in (None, "HT813"):
            raise ValueError("首版仅支持 HT813 型号默认配置")
        return self._publish("cfght813.xml" if model else "cfg.xml", configuration_xml(values))
