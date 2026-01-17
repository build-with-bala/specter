"""Human behaviour simulation for browser automation.

Replaces robotic instant actions with patterns that mimic a real
person sitting at a browser:

  * **Mouse movement** -- Bezier-curve paths with micro-jitter, not
    straight lines.  Overshoot correction on small targets.
  * **Typing** -- variable inter-key delay, occasional typo + backspace,
    burst/pause rhythm.
  * **Scrolling** -- smooth ease-in-out with random momentum.
  * **Micro-pauses** -- random short delays between actions to simulate
    "thinking" time.

All helpers are async and operate directly on a ``CDPSession`` so
they compose cleanly with the rest of Specter.

Usage::

    from specter.stealth.human import HumanInput
    human = HumanInput(cdp_session)
    await human.move_to(500, 300)
    await human.click()
    await human.type_text("hello world")
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import string
from typing import Any

from specter.core.cdp_client import CDPSession

logger = logging.getLogger(__name__)


# ── math helpers ──────────────────────────────────────────────────

def _bezier_point(t: float, p0: tuple[float, float], p1: tuple[float, float],
                  p2: tuple[float, float], p3: tuple[float, float]) -> tuple[float, float]:
    """Evaluate a cubic Bezier curve at parameter *t* in [0, 1]."""
    u = 1.0 - t
    x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


def _generate_bezier_path(
    start: tuple[float, float],
    end: tuple[float, float],
    steps: int = 25,
) -> list[tuple[float, float]]:
    """Create a human-like curved mouse path using a cubic Bezier.

    Two random control points are placed to create a natural arc.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    dist = math.hypot(dx, dy)

    # Control point spread scales with distance.
    spread = max(40.0, dist * 0.3)

    cp1 = (
        start[0] + dx * 0.25 + random.uniform(-spread, spread),
        start[1] + dy * 0.25 + random.uniform(-spread, spread),
    )
    cp2 = (
        start[0] + dx * 0.75 + random.uniform(-spread, spread),
        start[1] + dy * 0.75 + random.uniform(-spread, spread),
    )

    path: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        # Ease-in-out parameterisation.
        t_ease = 0.5 - 0.5 * math.cos(t * math.pi)
        px, py = _bezier_point(t_ease, start, cp1, cp2, end)
        # Add micro-jitter (1-2 pixels).
        jitter = max(0.5, dist * 0.005)
        px += random.gauss(0, jitter)
        py += random.gauss(0, jitter)
        path.append((px, py))

    # Ensure the final point is exactly the target.
    path[-1] = end
    return path


def _overshoot_path(
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    """Generate a path that slightly overshoots then corrects.

    Used for small targets where humans tend to overshoot.
    """
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    overshoot_factor = random.uniform(1.05, 1.15)
    overshoot = (
        end[0] + dx * (overshoot_factor - 1),
        end[1] + dy * (overshoot_factor - 1),
    )
    # First leg: move past the target.
    path = _generate_bezier_path(start, overshoot, steps=20)
    # Second leg: correct back.
    path.extend(_generate_bezier_path(overshoot, end, steps=8))
    return path


# ── typing helpers ────────────────────────────────────────────────

def _typing_delay(base: float) -> float:
    """Return a variable inter-keystroke delay."""
    # Simulate burst typing with occasional pauses.
    if random.random() < 0.05:
        # Thinking pause.
        return base * random.uniform(3.0, 8.0)
    if random.random() < 0.15:
        # Slightly slower.
        return base * random.uniform(1.5, 2.5)
    # Normal burst.
    return base * random.uniform(0.6, 1.4)


def _should_typo() -> bool:
    """Randomly decide whether to make a typing mistake."""
    return random.random() < 0.04  # ~4% error rate


def _nearby_key(char: str) -> str:
    """Return a plausible mistype of *char* based on keyboard proximity."""
    proximity: dict[str, str] = {
        "a": "sq", "b": "vn", "c": "xv", "d": "sf", "e": "wr",
        "f": "dg", "g": "fh", "h": "gj", "i": "uo", "j": "hk",
        "k": "jl", "l": "k;", "m": "n,", "n": "bm", "o": "ip",
        "p": "o[", "q": "wa", "r": "et", "s": "ad", "t": "ry",
        "u": "yi", "v": "cb", "w": "qe", "x": "zc", "y": "tu",
        "z": "xa",
    }
    neighbors = proximity.get(char.lower(), "")
    if neighbors:
        return random.choice(neighbors)
    return random.choice(string.ascii_lowercase)


# ── scroll helpers ────────────────────────────────────────────────

def _smooth_scroll_deltas(total_dy: int, steps: int = 8) -> list[int]:
    """Split a scroll distance into ease-in-out chunks."""
    deltas: list[int] = []
    for i in range(steps):
        t = (i + 1) / steps
        # Ease-in-out fraction.
        frac = 0.5 - 0.5 * math.cos(t * math.pi)
        prev = 0.5 - 0.5 * math.cos(i / steps * math.pi)
        chunk = int(total_dy * (frac - prev))
        deltas.append(chunk)
    return deltas


# ── main class ────────────────────────────────────────────────────

class HumanInput:
    """Simulate human-like input events through CDP.

    Parameters
    ----------
    session:
        Active CDP session.
    speed:
        Global speed multiplier.  1.0 = default speed, 0.5 = double
        speed, 2.0 = half speed (more cautious).
    """

    def __init__(self, session: CDPSession, *, speed: float = 1.0):
        self._cdp = session
        self._speed = max(0.1, speed)
        # Track the virtual cursor position.
        self._cursor_x: float = random.uniform(400, 800)
        self._cursor_y: float = random.uniform(300, 500)

    # ── mouse movement ────────────────────────────────────────────

    async def move_to(self, x: float, y: float) -> None:
        """Move the mouse cursor to ``(x, y)`` along a Bezier curve."""
        start = (self._cursor_x, self._cursor_y)
        end = (float(x), float(y))
        dist = math.hypot(end[0] - start[0], end[1] - start[1])

        # Choose movement style based on distance.
        if dist < 5:
            # Already there.
            self._cursor_x, self._cursor_y = end
            return

        if dist < 50 and random.random() < 0.3:
            path = _overshoot_path(start, end)
        else:
            num_steps = max(10, min(40, int(dist / 15)))
            path = _generate_bezier_path(start, end, steps=num_steps)

        # Traverse the path.
        base_delay = 0.008 * self._speed
        for px, py in path:
            await self._dispatch_mouse("mouseMoved", px, py)
            await asyncio.sleep(base_delay + random.uniform(0, 0.004) * self._speed)

        self._cursor_x, self._cursor_y = end

    async def click(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        button: str = "left",
        double: bool = False,
    ) -> None:
        """Click at the given position (or current cursor position).

        If *x* and *y* are provided, the cursor is moved there first
        using a Bezier path.
        """
        if x is not None and y is not None:
            await self.move_to(x, y)

        cx, cy = self._cursor_x, self._cursor_y

        # Small pre-click pause (human reaction).
        await asyncio.sleep(random.uniform(0.02, 0.08) * self._speed)

        clicks = 2 if double else 1
        for i in range(clicks):
            await self._dispatch_mouse("mousePressed", cx, cy, button=button, click_count=i + 1)
            await asyncio.sleep(random.uniform(0.04, 0.12) * self._speed)
            await self._dispatch_mouse("mouseReleased", cx, cy, button=button, click_count=i + 1)
            if i < clicks - 1:
                await asyncio.sleep(random.uniform(0.04, 0.10) * self._speed)

    async def right_click(self, x: float | None = None, y: float | None = None) -> None:
        """Right-click at the given position."""
        await self.click(x, y, button="right")

    # ── typing ────────────────────────────────────────────────────

    async def type_text(
        self,
        text: str,
        *,
        base_delay: float = 0.07,
        mistakes: bool = True,
    ) -> None:
        """Type *text* character by character with human-like timing.

        Parameters
        ----------
        text:
            The string to type.
        base_delay:
            Average inter-key delay in seconds.
        mistakes:
            If ``True``, occasionally introduce a typo and backspace.
        """
        for i, char in enumerate(text):
            # Possible typo.
            if mistakes and char.isalpha() and _should_typo():
                wrong = _nearby_key(char)
                await self._type_char(wrong)
                await asyncio.sleep(_typing_delay(base_delay) * self._speed)
                # Pause before correcting.
                await asyncio.sleep(random.uniform(0.15, 0.4) * self._speed)
                await self._press_key("Backspace")
                await asyncio.sleep(random.uniform(0.05, 0.15) * self._speed)

            await self._type_char(char)
            await asyncio.sleep(_typing_delay(base_delay) * self._speed)

    async def press_key(self, key: str) -> None:
        """Press a single key with human-like timing."""
        await asyncio.sleep(random.uniform(0.02, 0.06) * self._speed)
        await self._press_key(key)

    # ── scrolling ─────────────────────────────────────────────────

    async def scroll(self, dy: int = 400, *, dx: int = 0) -> None:
        """Smooth scroll with ease-in-out timing.

        Parameters
        ----------
        dy:
            Vertical scroll amount in pixels (positive = down).
        dx:
            Horizontal scroll amount in pixels (positive = right).
        """
        deltas = _smooth_scroll_deltas(dy)
        for delta in deltas:
            await self._cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": self._cursor_x,
                "y": self._cursor_y,
                "deltaX": dx // len(deltas) if dx else 0,
                "deltaY": delta,
            })
            await asyncio.sleep(random.uniform(0.02, 0.06) * self._speed)

        # Random "momentum" extra ticks.
        if abs(dy) > 200 and random.random() < 0.4:
            extra = random.randint(1, 3)
            for _ in range(extra):
                tiny = random.randint(5, 30) * (1 if dy > 0 else -1)
                await self._cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel",
                    "x": self._cursor_x,
                    "y": self._cursor_y,
                    "deltaX": 0,
                    "deltaY": tiny,
                })
                await asyncio.sleep(random.uniform(0.04, 0.10) * self._speed)

    async def scroll_to_element(
        self,
        x: float, y: float,
        viewport_height: int = 900,
    ) -> None:
        """Scroll until the element at ``(x, y)`` is in the viewport."""
        if y < 0:
            await self.scroll(dy=int(y - viewport_height * 0.3))
        elif y > viewport_height:
            await self.scroll(dy=int(y - viewport_height * 0.3))

    # ── pauses ────────────────────────────────────────────────────

    async def micro_pause(self) -> None:
        """Random short pause simulating reading / thinking."""
        await asyncio.sleep(random.uniform(0.2, 0.8) * self._speed)

    async def short_pause(self) -> None:
        """Short thinking pause (1-3 seconds)."""
        await asyncio.sleep(random.uniform(1.0, 3.0) * self._speed)

    async def long_pause(self) -> None:
        """Longer pause simulating reading a page (3-8 seconds)."""
        await asyncio.sleep(random.uniform(3.0, 8.0) * self._speed)

    async def random_idle(self) -> None:
        """Simulate idle behaviour: random mouse wiggle + pause."""
        wiggle_x = self._cursor_x + random.uniform(-30, 30)
        wiggle_y = self._cursor_y + random.uniform(-20, 20)
        await self.move_to(wiggle_x, wiggle_y)
        await self.micro_pause()

    # ── CDP dispatch helpers ──────────────────────────────────────

    async def _dispatch_mouse(
        self, kind: str, x: float, y: float,
        *, button: str = "left", click_count: int = 0,
    ) -> None:
        params: dict[str, Any] = {"type": kind, "x": x, "y": y}
        if kind in ("mousePressed", "mouseReleased"):
            params["button"] = button
            params["clickCount"] = click_count
        await self._cdp.send("Input.dispatchMouseEvent", params)

    async def _type_char(self, char: str) -> None:
        """Dispatch key events for a single character."""
        await self._cdp.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "text": char, "key": char,
            "unmodifiedText": char,
        })
        await self._cdp.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": char,
        })

    async def _press_key(self, key: str) -> None:
        """Press and release a named key (Enter, Tab, Backspace, etc.)."""
        specials = {"Enter": "\r", "Tab": "\t", "Escape": "\x1b", "Backspace": "\b"}
        await self._cdp.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": key, "text": specials.get(key, ""),
        })
        await self._cdp.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": key,
        })
