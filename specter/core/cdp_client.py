"""Chrome DevTools Protocol client — raw WebSocket JSON-RPC transport.

This is the lowest layer of Specter.  Every browser interaction ultimately
passes through here as a ``{id, method, params}`` message sent over a
single WebSocket connection to Chrome's ``/devtools/page/<id>`` endpoint.

Design decisions
~~~~~~~~~~~~~~~~
* One CDPSession per browser tab (matches CDP's own model).
* Incoming messages are split into *responses* (have ``id``) and *events*
  (have ``method``).  Responses resolve the matching ``asyncio.Future``;
  events are dispatched to registered callbacks.
* The receive loop runs as a background task so callers never block on I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger(__name__)

EventHandler = Callable[..., None] | Callable[..., Coroutine]


class CDPError(Exception):
    """Raised when the browser returns an error response."""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.data = data
        super().__init__(f"CDP {code}: {message}")


class CDPSession:
    """Bi-directional CDP channel bound to a single target (tab).

    Usage::

        session = CDPSession("ws://localhost:9222/devtools/page/ABC")
        await session.connect()

        await session.send("Page.enable")
        result = await session.send("Runtime.evaluate",
                                    {"expression": "1 + 1"})

        session.on("Page.loadEventFired", lambda p: print("loaded"))
        await session.disconnect()
    """

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws: ClientConnection | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._handlers: dict[str, list[EventHandler]] = {}
        self._loop_task: asyncio.Task[None] | None = None

    # ── lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self.ws_url,
            max_size=100 * 1024 * 1024,
            ping_interval=30,
            ping_timeout=10,
        )
        self._loop_task = asyncio.create_task(self._recv_loop())
        logger.debug("CDP connected → %s", self.ws_url[:80])

    async def disconnect(self) -> None:
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        self._pending.clear()

    # ── command interface ──────────────────────────────────────────

    async def send(self, method: str, params: dict[str, Any] | None = None,
                   *, timeout: float = 30) -> Any:
        """Send a CDP command and await the result dict."""
        assert self._ws, "Not connected"

        self._next_id += 1
        mid = self._next_id
        msg: dict[str, Any] = {"id": mid, "method": method}
        if params:
            msg["params"] = params

        fut: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[mid] = fut

        await self._ws.send(json.dumps(msg))

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(mid, None)
            raise CDPError(-1, f"Timeout ({timeout}s) for {method}")

    # ── event interface ────────────────────────────────────────────

    def on(self, event: str, handler: EventHandler) -> None:
        self._handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: EventHandler | None = None) -> None:
        if handler is None:
            self._handlers.pop(event, None)
        elif event in self._handlers:
            self._handlers[event] = [h for h in self._handlers[event] if h is not handler]

    async def wait_for_event(self, event: str, *, timeout: float = 30) -> dict:
        """Block until a specific CDP event fires, then return its params."""
        fut: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

        def _once(params: dict) -> None:
            if not fut.done():
                fut.set_result(params)

        self.on(event, _once)
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.off(event, _once)

    # ── internal ───────────────────────────────────────────────────

    async def _recv_loop(self) -> None:
        assert self._ws
        try:
            async for raw in self._ws:
                msg = json.loads(raw)

                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            e = msg["error"]
                            fut.set_exception(CDPError(e.get("code", -1), e.get("message", "")))
                        else:
                            fut.set_result(msg.get("result", {}))

                elif "method" in msg:
                    for handler in self._handlers.get(msg["method"], []):
                        try:
                            ret = handler(msg.get("params", {}))
                            if asyncio.iscoroutine(ret):
                                asyncio.create_task(ret)
                        except Exception:
                            logger.exception("handler error for %s", msg["method"])

        except websockets.ConnectionClosed:
            logger.warning("CDP connection closed")
        except asyncio.CancelledError:
            pass


class CDPClient:
    """High-level helper to discover targets and open sessions.

    Usage::

        client = CDPClient("http://localhost:9222")
        session = await client.connect_tab()
    """

    def __init__(self, debug_url: str = "http://localhost:9222"):
        self.debug_url = debug_url.rstrip("/")

    async def connect_tab(self, target_id: str | None = None) -> CDPSession:
        """Connect to an existing (or the first) browser tab."""
        import aiohttp

        async with aiohttp.ClientSession() as http:
            if target_id:
                ws = f"{self.debug_url}/devtools/page/{target_id}"
            else:
                async with http.get(f"{self.debug_url}/json") as r:
                    targets = await r.json()
                pages = [t for t in targets if t.get("type") == "page"]
                if not pages:
                    async with http.get(f"{self.debug_url}/json/new") as r:
                        pages = [await r.json()]
                ws = pages[0]["webSocketDebuggerUrl"]

        sess = CDPSession(ws)
        await sess.connect()
        for domain in ("Page", "Runtime", "DOM", "Network", "CSS"):
            await sess.send(f"{domain}.enable")
        return sess

    async def new_tab(self, url: str = "about:blank") -> CDPSession:
        import aiohttp
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{self.debug_url}/json/new?{url}") as r:
                target = await r.json()
        sess = CDPSession(target["webSocketDebuggerUrl"])
        await sess.connect()
        for domain in ("Page", "Runtime", "Network"):
            await sess.send(f"{domain}.enable")
        return sess

    async def list_targets(self) -> list[dict]:
        import aiohttp
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{self.debug_url}/json") as r:
                return await r.json()

    async def close_target(self, target_id: str) -> None:
        import aiohttp
        async with aiohttp.ClientSession() as http:
            await http.get(f"{self.debug_url}/json/close/{target_id}")

    async def version(self) -> dict:
        import aiohttp
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{self.debug_url}/json/version") as r:
                return await r.json()
