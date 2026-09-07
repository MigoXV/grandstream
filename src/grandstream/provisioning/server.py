"""Optional ASGI provisioning application. No directory browsing or write API."""

import asyncio
import os
import re
import stat
from pathlib import Path


def create_app(directory: str | Path):
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Route

    root = Path(directory).resolve()

    def read_config(name):
        # O_NOFOLLOW closes the symlink race between a path check and open.
        fd = os.open(root / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as file:
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                raise OSError("Not a regular file")
            return file.read()

    async def fetch(request):
        name = request.path_params["name"]
        if not re.fullmatch(r"cfg(?:[0-9a-f]{12}|ht813)?\.xml", name):
            return Response(status_code=404)
        try:
            body = await asyncio.to_thread(read_config, name)
        except OSError:
            return Response(status_code=404)
        return Response(body, media_type="application/xml", headers={"Cache-Control": "no-store"})

    return Starlette(routes=[Route("/{name}", fetch, methods=["GET", "HEAD"])])
