"""Page — high-level controller for a single browser tab.

Wraps a ``CDPSession`` and exposes the developer-facing API for
navigation, DOM queries, input simulation, screenshots, cookies,
and JavaScript evaluation.  Heavier features (network interception,
stealth, AI selectors) live in their own subpackages and accept a
Page or CDPSession as an argument so this module stays focused.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Any

from specter.core.cdp_client import CDPSession
from specter.core.types import Box, Cookie, ElementHandle, Point, WaitUntil

logger = logging.getLogger(__name__)


class Page:
    """Controls a single browser tab."""

    def __init__(self, session: CDPSession):
        self.cdp = session
        self._url = ""
        self._nav_events: list[asyncio.Event] = []

        self.cdp.on("Page.loadEventFired", self._on_load)
        self.cdp.on("Page.frameNavigated", self._on_frame)

    # ── navigation ─────────────────────────────────────────────────

    async def goto(self, url: str, *, wait: WaitUntil = WaitUntil.LOAD,
                   timeout: float = 30) -> None:
        ev = asyncio.Event()
        self._nav_events.append(ev)
        try:
            r = await self.cdp.send("Page.navigate", {"url": url})
            if "errorText" in r:
                raise RuntimeError(f"Navigation failed: {r['errorText']}")
            if wait == WaitUntil.LOAD:
                await asyncio.wait_for(ev.wait(), timeout)
            elif wait == WaitUntil.NETWORKIDLE:
                await self._wait_network_idle(timeout)
            self._url = url
        finally:
            self._nav_events.remove(ev)

    async def reload(self, *, ignore_cache: bool = False) -> None:
        await self.cdp.send("Page.reload", {"ignoreCache": ignore_cache})
        await asyncio.sleep(1.0)

    async def go_back(self) -> None:
        h = await self.cdp.send("Page.getNavigationHistory")
        idx = h.get("currentIndex", 0)
        if idx > 0:
            await self.cdp.send("Page.navigateToHistoryEntry",
                                {"entryId": h["entries"][idx - 1]["id"]})

    async def go_forward(self) -> None:
        h = await self.cdp.send("Page.getNavigationHistory")
        idx, entries = h.get("currentIndex", 0), h.get("entries", [])
        if idx < len(entries) - 1:
            await self.cdp.send("Page.navigateToHistoryEntry",
                                {"entryId": entries[idx + 1]["id"]})

    @property
    def url(self) -> str:
        return self._url

    async def title(self) -> str:
        return await self.evaluate("document.title") or ""

    # ── JavaScript ─────────────────────────────────────────────────

    async def evaluate(self, expression: str, *, await_promise: bool = True) -> Any:
        r = await self.cdp.send("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in r:
            raise RuntimeError(r["exceptionDetails"].get("text", "JS Error"))
        return r.get("result", {}).get("value")

    async def evaluate_handle(self, expression: str) -> str:
        """Return a remote-object handle instead of a value."""
        r = await self.cdp.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": False,
        })
        return r.get("result", {}).get("objectId", "")

    # ── DOM queries ────────────────────────────────────────────────

    async def query(self, selector: str) -> ElementHandle | None:
        """CSS selector → single element (or None)."""
        root = await self._root_node_id()
        r = await self.cdp.send("DOM.querySelector",
                                {"nodeId": root, "selector": selector})
        nid = r.get("nodeId", 0)
        return await self._describe(nid) if nid else None

    async def query_all(self, selector: str) -> list[ElementHandle]:
        root = await self._root_node_id()
        r = await self.cdp.send("DOM.querySelectorAll",
                                {"nodeId": root, "selector": selector})
        out: list[ElementHandle] = []
        for nid in r.get("nodeIds", []):
            el = await self._describe(nid)
            if el:
                out.append(el)
        return out

    async def xpath(self, expression: str) -> list[ElementHandle]:
        """XPath → list of elements."""
        obj_id = await self.evaluate_handle(
            f'document.evaluate(`{expression}`, document, null, '
            f'XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null)'
        )
        if not obj_id:
            return []
        length = await self.evaluate(
            f'document.evaluate(`{expression}`, document, null, '
            f'XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength'
        )
        elements = []
        for i in range(length or 0):
            nid_r = await self.cdp.send("Runtime.evaluate", {
                "expression": f'document.evaluate(`{expression}`, document, null, '
                              f'XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null)'
                              f'.snapshotItem({i})',
                "returnByValue": False,
            })
            oid = nid_r.get("result", {}).get("objectId")
            if oid:
                node = await self.cdp.send("DOM.requestNode", {"objectId": oid})
                el = await self._describe(node.get("nodeId", 0))
                if el:
                    elements.append(el)
        return elements

    # ── input ──────────────────────────────────────────────────────

    async def click(self, selector: str, *, button: str = "left",
                    count: int = 1) -> None:
        el = await self._require(selector)
        assert el.box, f"Element has no layout: {selector}"
        cx, cy = el.box.center.x, el.box.center.y
        await self._dispatch_mouse("mouseMoved", cx, cy)
        await asyncio.sleep(0.04)
        for _ in range(count):
            await self._dispatch_mouse("mousePressed", cx, cy, button=button, click=1)
            await asyncio.sleep(0.02)
            await self._dispatch_mouse("mouseReleased", cx, cy, button=button, click=1)

    async def type_text(self, selector: str, text: str, *,
                        delay: float = 0.05) -> None:
        await self.click(selector)
        await asyncio.sleep(0.08)
        for ch in text:
            await self.cdp.send("Input.dispatchKeyEvent",
                                {"type": "keyDown", "text": ch, "key": ch})
            await self.cdp.send("Input.dispatchKeyEvent",
                                {"type": "keyUp", "key": ch})
            if delay:
                await asyncio.sleep(delay)

    async def fill(self, selector: str, value: str) -> None:
        """Set an input's value programmatically (fast, no keystrokes)."""
        await self.click(selector)
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        await self.evaluate(f'''(() => {{
            const el = document.querySelector("{selector}");
            el.value = "{escaped}";
            el.dispatchEvent(new Event("input",  {{bubbles:true}}));
            el.dispatchEvent(new Event("change", {{bubbles:true}}));
        }})()''')

    async def press_key(self, key: str) -> None:
        specials = {"Enter": "\r", "Tab": "\t", "Escape": "\x1b", "Backspace": "\b"}
        await self.cdp.send("Input.dispatchKeyEvent",
                            {"type": "keyDown", "key": key, "text": specials.get(key, "")})
        await self.cdp.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": key})

    async def scroll(self, dx: int = 0, dy: int = 500) -> None:
        await self.cdp.send("Input.dispatchMouseEvent", {
            "type": "mouseWheel", "x": 960, "y": 540,
            "deltaX": dx, "deltaY": dy,
        })
        await asyncio.sleep(0.25)

    async def hover(self, selector: str) -> None:
        el = await self._require(selector)
        assert el.box
        await self._dispatch_mouse("mouseMoved", el.box.center.x, el.box.center.y)

    async def select(self, selector: str, value: str) -> None:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        await self.evaluate(f'''(() => {{
            const s = document.querySelector("{selector}");
            s.value = "{escaped}";
            s.dispatchEvent(new Event("change", {{bubbles:true}}));
        }})()''')

    async def check(self, selector: str) -> None:
        checked = await self.evaluate(f'document.querySelector("{selector}").checked')
        if not checked:
            await self.click(selector)

    async def uncheck(self, selector: str) -> None:
        checked = await self.evaluate(f'document.querySelector("{selector}").checked')
        if checked:
            await self.click(selector)

    # ── screenshots / PDF ──────────────────────────────────────────

    async def screenshot(self, path: str | None = None, *,
                         full_page: bool = False) -> bytes:
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            m = await self.cdp.send("Page.getLayoutMetrics")
            cs = m.get("contentSize", {})
            params["clip"] = {"x": 0, "y": 0, "width": cs.get("width", 1920),
                              "height": cs.get("height", 1080), "scale": 1}
        r = await self.cdp.send("Page.captureScreenshot", params)
        data = base64.b64decode(r["data"])
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
        return data

    async def pdf(self, path: str) -> bytes:
        r = await self.cdp.send("Page.printToPDF",
                                {"printBackground": True, "preferCSSPageSize": True})
        data = base64.b64decode(r["data"])
        Path(path).write_bytes(data)
        return data

    # ── cookies ────────────────────────────────────────────────────

    async def cookies(self) -> list[Cookie]:
        r = await self.cdp.send("Network.getCookies")
        return [Cookie(name=c["name"], value=c["value"], domain=c.get("domain", ""),
                       path=c.get("path", "/"), expires=c.get("expires", -1),
                       http_only=c.get("httpOnly", False), secure=c.get("secure", False),
                       same_site=c.get("sameSite", "Lax"))
                for c in r.get("cookies", [])]

    async def set_cookies(self, cookies: list[Cookie]) -> None:
        await self.cdp.send("Network.setCookies",
                            {"cookies": [c.to_cdp_param() for c in cookies]})

    async def clear_cookies(self) -> None:
        await self.cdp.send("Network.clearBrowserCookies")

    # ── waiting ────────────────────────────────────────────────────

    async def wait_for(self, selector: str, *, timeout: float = 10,
                       visible: bool = True) -> ElementHandle:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            el = await self.query(selector)
            if el and (not visible or el.visible):
                return el
            await asyncio.sleep(0.15)
        raise TimeoutError(f"wait_for({selector!r}) timed out after {timeout}s")

    async def wait_for_navigation(self, *, timeout: float = 30) -> None:
        ev = asyncio.Event()
        self._nav_events.append(ev)
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        finally:
            self._nav_events.remove(ev)

    async def wait_for_function(self, expr: str, *, timeout: float = 10) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            v = await self.evaluate(expr)
            if v:
                return v
            await asyncio.sleep(0.15)
        raise TimeoutError(f"wait_for_function timed out after {timeout}s")

    # ── page content ───────────────────────────────────────────────

    async def content(self) -> str:
        doc = await self.cdp.send("DOM.getDocument", {"depth": -1})
        r = await self.cdp.send("DOM.getOuterHTML", {"nodeId": doc["root"]["nodeId"]})
        return r.get("outerHTML", "")

    async def set_content(self, html: str) -> None:
        doc = await self.cdp.send("DOM.getDocument")
        await self.cdp.send("DOM.setOuterHTML",
                            {"nodeId": doc["root"]["nodeId"], "outerHTML": html})

    async def text_content(self) -> str:
        return await self.evaluate("document.body.innerText") or ""

    # ── viewport / emulation ───────────────────────────────────────

    async def set_viewport(self, width: int, height: int,
                           device_scale: float = 1.0, mobile: bool = False) -> None:
        await self.cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height,
            "deviceScaleFactor": device_scale, "mobile": mobile,
        })

    async def set_user_agent(self, ua: str) -> None:
        await self.cdp.send("Network.setUserAgentOverride", {"userAgent": ua})

    async def set_geolocation(self, lat: float, lon: float, accuracy: float = 1) -> None:
        await self.cdp.send("Emulation.setGeolocationOverride",
                            {"latitude": lat, "longitude": lon, "accuracy": accuracy})

    # ── internals ──────────────────────────────────────────────────

    async def _root_node_id(self) -> int:
        doc = await self.cdp.send("DOM.getDocument", {"depth": 0})
        return doc["root"]["nodeId"]

    async def _describe(self, node_id: int) -> ElementHandle | None:
        if not node_id:
            return None
        try:
            d = await self.cdp.send("DOM.describeNode", {"nodeId": node_id})
            n = d.get("node", {})
            obj = await self.cdp.send("DOM.resolveNode", {"nodeId": node_id})
            oid = obj.get("object", {}).get("objectId", "")

            box = None
            try:
                bm = await self.cdp.send("DOM.getBoxModel", {"nodeId": node_id})
                c = bm.get("model", {}).get("content", [])
                if len(c) >= 8:
                    box = Box(x=c[0], y=c[1], width=c[4] - c[0], height=c[5] - c[1])
            except Exception:
                pass

            attrs: dict[str, str] = {}
            raw = n.get("attributes", [])
            for i in range(0, len(raw) - 1, 2):
                attrs[raw[i]] = raw[i + 1]

            return ElementHandle(
                node_id=node_id, backend_node_id=n.get("backendNodeId", 0),
                object_id=oid, tag=n.get("nodeName", ""), attrs=attrs,
                box=box,
            )
        except Exception:
            return None

    async def _require(self, selector: str) -> ElementHandle:
        el = await self.query(selector)
        if not el:
            raise RuntimeError(f"Element not found: {selector}")
        return el

    async def _dispatch_mouse(self, kind: str, x: float, y: float, *,
                              button: str = "left", click: int = 0) -> None:
        p: dict[str, Any] = {"type": kind, "x": x, "y": y}
        if kind in ("mousePressed", "mouseReleased"):
            p["button"] = button
            p["clickCount"] = click
        await self.cdp.send("Input.dispatchMouseEvent", p)

    async def _wait_network_idle(self, timeout: float, idle: float = 0.5) -> None:
        pending = 0
        last_activity = time.monotonic()

        def on_req(_: Any) -> None:
            nonlocal pending, last_activity
            pending += 1
            last_activity = time.monotonic()

        def on_done(_: Any) -> None:
            nonlocal pending, last_activity
            pending = max(0, pending - 1)
            if pending == 0:
                last_activity = time.monotonic()

        self.cdp.on("Network.requestWillBeSent", on_req)
        self.cdp.on("Network.loadingFinished", on_done)
        self.cdp.on("Network.loadingFailed", on_done)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if pending == 0 and time.monotonic() - last_activity > idle:
                    return
                await asyncio.sleep(0.1)
        finally:
            self.cdp.off("Network.requestWillBeSent", on_req)
            self.cdp.off("Network.loadingFinished", on_done)
            self.cdp.off("Network.loadingFailed", on_done)

    def _on_load(self, _: dict) -> None:
        for ev in self._nav_events:
            ev.set()

    def _on_frame(self, params: dict) -> None:
        frame = params.get("frame", {})
        if not frame.get("parentId"):
            self._url = frame.get("url", self._url)
