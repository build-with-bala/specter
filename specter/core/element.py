"""Element — chainable wrapper around an ElementHandle + its owning Page.

Instead of passing selectors every time, callers can grab an Element
once and call methods directly::

    btn = await page.get("#submit")
    await btn.click()
    await btn.screenshot("button.png")

The Element keeps a reference to the Page so it can re-query itself
if the DOM mutates (self-healing selector pattern).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from specter.core.types import Box, ElementHandle, Point

if TYPE_CHECKING:
    from specter.core.page import Page


class Element:
    """Wraps an ``ElementHandle`` with a back-reference to its ``Page``."""

    def __init__(self, handle: ElementHandle, page: "Page",
                 original_selector: str = ""):
        self._h = handle
        self._page = page
        self._selector = original_selector or handle.css_selector

    # ── properties ─────────────────────────────────────────────────

    @property
    def tag(self) -> str:
        return self._h.tag

    @property
    def text(self) -> str:
        return self._h.text

    @property
    def attrs(self) -> dict[str, str]:
        return self._h.attrs

    @property
    def box(self) -> Box | None:
        return self._h.box

    @property
    def visible(self) -> bool:
        return self._h.visible

    @property
    def node_id(self) -> int:
        return self._h.node_id

    # ── interactions ───────────────────────────────────────────────

    async def click(self, **kw: Any) -> "Element":
        await self._page.click(self._selector, **kw)
        return self

    async def type_text(self, text: str, **kw: Any) -> "Element":
        await self._page.type_text(self._selector, text, **kw)
        return self

    async def fill(self, value: str) -> "Element":
        await self._page.fill(self._selector, value)
        return self

    async def hover(self) -> "Element":
        await self._page.hover(self._selector)
        return self

    async def check(self) -> "Element":
        await self._page.check(self._selector)
        return self

    async def uncheck(self) -> "Element":
        await self._page.uncheck(self._selector)
        return self

    async def select(self, value: str) -> "Element":
        await self._page.select(self._selector, value)
        return self

    async def press(self, key: str) -> "Element":
        await self.click()
        await self._page.press_key(key)
        return self

    async def screenshot(self, path: str) -> bytes:
        """Screenshot just this element by clipping to its bounding box."""
        assert self._h.box, "Element has no layout"
        b = self._h.box
        r = await self._page.cdp.send("Page.captureScreenshot", {
            "format": "png",
            "clip": {"x": b.x, "y": b.y, "width": b.width,
                     "height": b.height, "scale": 1},
        })
        import base64
        from pathlib import Path
        data = base64.b64decode(r["data"])
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)
        return data

    # ── queries ────────────────────────────────────────────────────

    async def get_attribute(self, name: str) -> str | None:
        return self._h.attrs.get(name)

    async def inner_text(self) -> str:
        return await self._page.evaluate(
            f'document.querySelector("{self._selector}").innerText'
        ) or ""

    async def inner_html(self) -> str:
        return await self._page.evaluate(
            f'document.querySelector("{self._selector}").innerHTML'
        ) or ""

    async def value(self) -> str:
        return await self._page.evaluate(
            f'document.querySelector("{self._selector}").value'
        ) or ""

    async def is_visible(self) -> bool:
        return await self._page.evaluate(f'''(() => {{
            const el = document.querySelector("{self._selector}");
            if (!el) return false;
            const s = getComputedStyle(el);
            return s.display !== "none" && s.visibility !== "hidden"
                   && s.opacity !== "0" && el.offsetParent !== null;
        }})()''') or False

    async def is_enabled(self) -> bool:
        return await self._page.evaluate(
            f'!document.querySelector("{self._selector}").disabled'
        ) or False

    async def is_checked(self) -> bool:
        return await self._page.evaluate(
            f'document.querySelector("{self._selector}").checked'
        ) or False

    # ── child queries ──────────────────────────────────────────────

    async def query(self, selector: str) -> "Element | None":
        full = f"{self._selector} {selector}"
        h = await self._page.query(full)
        return Element(h, self._page, full) if h else None

    async def query_all(self, selector: str) -> list["Element"]:
        full = f"{self._selector} {selector}"
        handles = await self._page.query_all(full)
        return [Element(h, self._page, full) for h in handles]

    # ── self-healing ───────────────────────────────────────────────

    async def refresh(self) -> bool:
        """Re-query the DOM for this element.  Returns False if gone."""
        h = await self._page.query(self._selector)
        if h:
            self._h = h
            return True
        return False

    def __repr__(self) -> str:
        return f"<Element {self._h.tag} selector={self._selector!r}>"
