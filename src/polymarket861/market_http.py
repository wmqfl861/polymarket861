"""A pooled public HTTP client with a deadline covering connect, queueing and the last byte."""

import asyncio
import os
import ssl
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import httpx

from . import store

_MAX_BODY = 32 * 1024 * 1024
_owner = None
_lock = threading.Lock()


class _Transport:
    def __init__(self):
        self.pid = os.getpid()
        self.loop = asyncio.new_event_loop()
        self.clients = {}
        threading.Thread(
            target=self.loop.run_forever, name="pm861-public-http", daemon=True
        ).start()

    async def read(self, method, url, headers, content, until):
        timeout = until - time.monotonic()
        if timeout <= 0:
            raise TimeoutError("public HTTP total deadline elapsed")
        try:
            async with asyncio.timeout(timeout):
                uri = urlsplit(url)
                proxies = getproxies()
                proxy = (
                    None
                    if proxy_bypass(uri.hostname)
                    else proxies.get(uri.scheme) or proxies.get("all")
                )
                if proxy not in self.clients:
                    self.clients[proxy] = httpx.AsyncClient(
                        proxy=proxy,
                        trust_env=False,
                        verify=ssl.create_default_context(),
                        limits=httpx.Limits(
                            max_connections=64,
                            max_keepalive_connections=24,
                            keepalive_expiry=60,
                        ),
                        timeout=None,
                        follow_redirects=True,
                        max_redirects=5,
                    )
                requested = store.now_utc()
                async with self.clients[proxy].stream(
                    method, url, headers=headers, content=content
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise HTTPError(
                            url,
                            response.status_code,
                            response.reason_phrase,
                            response.headers,
                            None,
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_BODY:
                            raise ValueError(
                                "payload exceeds 32 MiB; reduce the page size"
                            )
                        body.extend(chunk)
                    received = store.now_utc()
                    return (
                        bytes(body),
                        requested,
                        received,
                        dict(response.headers),
                        str(response.url),
                    )
        except httpx.HTTPError as exc:
            raise OSError(f"public HTTP read failed: {exc}") from exc


def read(method, url, headers, content, timeout):
    global _owner
    until = time.monotonic() + timeout
    with _lock:
        if _owner is None or _owner.pid != os.getpid():
            _owner = _Transport()
        owner = _owner
    task = asyncio.run_coroutine_threadsafe(
        owner.read(method, url, headers, content, until), owner.loop
    )
    try:
        return task.result(timeout=max(0, until - time.monotonic()))
    except FutureTimeout as exc:
        task.cancel()
        raise TimeoutError("public HTTP total deadline elapsed") from exc
