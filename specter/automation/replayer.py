"""Replay recorded actions with smart waiting and self-healing selectors.

Takes a list of ``RecordedAction`` objects (loaded from JSON via
``ActionRecorder.load()``) and re-executes them against a live page.

Key features:
  * **Smart waiting** -- before each action, waits for the target
    element to be visible and the DOM to be stable.
  * **Self-healing selectors** -- if a selector breaks (DOM changed
    since recording), tries alternative strategies to find the same
    element (by text, aria-label, nearby structure).
  * **Timing fidelity** -- respects relative timing between actions
    (optionally compressed or stretched via a speed factor).
  * **Error recovery** -- configurable: skip, retry, or abort on failure.

Usage::

    from specter.automation.recorder import ActionRecorder
    from specter.automation.replayer import ActionReplayer

    actions = ActionRecorder.load("session.json")
    replayer = ActionReplayer(page)
    report = await replayer.replay(actions)
    print(report.summary())
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from specter.core.page import Page
from specter.core.types import RecordedAction
from specter.automation.waits import (
    wait_for_dom_stable,
    wait_for_element_state,
    wait_for_network_idle,
)

logger = logging.getLogger(__name__)


# ── result tracking ──────────────────────────────────────────────

@dataclass
class StepResult:
    """Outcome of replaying a single action."""
    index: int
    action: RecordedAction
    success: bool
    selector_healed: bool = False
    healed_selector: str = ""
    error: str = ""
    duration_ms: float = 0.0


@dataclass
class ReplayReport:
    """Aggregate result of a full replay session."""
    steps: list[StepResult] = field(default_factory=list)
    total_duration_ms: float = 0.0

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.success)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if not s.success)

    @property
    def healed(self) -> int:
        return sum(1 for s in self.steps if s.selector_healed)

    def summary(self) -> str:
        return (
            f"Replay: {self.passed}/{len(self.steps)} passed, "
            f"{self.failed} failed, {self.healed} selectors healed, "
            f"{self.total_duration_ms:.0f}ms total"
        )


# ── replayer ──────────────────────────────────────────────────────

class ActionReplayer:
    """Replay recorded browser actions with resilience.

    Parameters
    ----------
    page:
        The ``Page`` to replay actions on.
    speed:
        Speed factor for inter-action delays.  1.0 = real-time,
        0.5 = double speed, 2.0 = half speed.
    on_error:
        Error handling strategy:
        - ``"skip"`` -- log and continue to the next action.
        - ``"retry"`` -- retry the action up to ``max_retries`` times.
        - ``"abort"`` -- stop the entire replay.
    max_retries:
        Number of retries when ``on_error="retry"``.
    """

    def __init__(
        self,
        page: Page,
        *,
        speed: float = 1.0,
        on_error: str = "retry",
        max_retries: int = 2,
        wait_timeout: float = 10.0,
    ):
        self._page = page
        self._speed = max(0.01, speed)
        self._on_error = on_error
        self._max_retries = max_retries
        self._wait_timeout = wait_timeout

    async def replay(
        self,
        actions: list[RecordedAction],
        *,
        start_index: int = 0,
        end_index: int | None = None,
    ) -> ReplayReport:
        """Replay a sequence of recorded actions.

        Parameters
        ----------
        actions:
            List of ``RecordedAction`` to replay.
        start_index:
            Index of the first action to replay (inclusive).
        end_index:
            Index of the last action to replay (exclusive).

        Returns
        -------
        A ``ReplayReport`` with step-by-step results.
        """
        report = ReplayReport()
        subset = actions[start_index:end_index]
        replay_start = time.monotonic()

        prev_ts: float | None = None

        for i, action in enumerate(subset):
            step_start = time.monotonic()

            # Inter-action delay.
            if prev_ts is not None and action.timestamp > 0 and prev_ts > 0:
                delta = (action.timestamp - prev_ts) * self._speed
                if 0 < delta < 10:
                    await asyncio.sleep(delta)
            prev_ts = action.timestamp

            step_result = StepResult(
                index=start_index + i,
                action=action,
                success=False,
            )

            try:
                await self._execute_action(action, step_result)
                step_result.success = True
            except Exception as e:
                step_result.error = str(e)
                logger.warning("Step %d (%s) failed: %s",
                               step_result.index, action.kind, e)

                if self._on_error == "retry":
                    for attempt in range(self._max_retries):
                        try:
                            await asyncio.sleep(0.5)
                            await self._execute_action(action, step_result)
                            step_result.success = True
                            step_result.error = ""
                            break
                        except Exception as retry_e:
                            step_result.error = str(retry_e)
                            logger.debug("Retry %d failed: %s", attempt + 1, retry_e)
                elif self._on_error == "abort":
                    step_result.duration_ms = (time.monotonic() - step_start) * 1000
                    report.steps.append(step_result)
                    break
                # "skip" -- just continue.

            step_result.duration_ms = (time.monotonic() - step_start) * 1000
            report.steps.append(step_result)

        report.total_duration_ms = (time.monotonic() - replay_start) * 1000
        logger.info(report.summary())
        return report

    # ── action dispatch ───────────────────────────────────────────

    async def _execute_action(
        self,
        action: RecordedAction,
        result: StepResult,
    ) -> None:
        """Execute a single recorded action."""
        kind = action.kind

        if kind == "navigate":
            await self._do_navigate(action)
        elif kind == "click":
            await self._do_click(action, result)
        elif kind == "type":
            await self._do_type(action, result)
        elif kind == "press_key":
            await self._do_press_key(action, result)
        elif kind == "scroll":
            await self._do_scroll(action)
        elif kind == "select":
            await self._do_select(action, result)
        elif kind == "check":
            await self._do_check(action, result)
        elif kind == "wait":
            await asyncio.sleep(action.meta.get("duration", 1.0))
        elif kind == "hover":
            await self._do_hover(action, result)
        else:
            logger.warning("Unknown action kind: %s", kind)

    # ── individual action handlers ────────────────────────────────

    async def _do_navigate(self, action: RecordedAction) -> None:
        if action.url and action.url != "about:blank":
            await self._page.goto(action.url)
            try:
                await wait_for_dom_stable(self._page, stability_ms=300, timeout=self._wait_timeout)
            except TimeoutError:
                pass

    async def _do_click(self, action: RecordedAction, result: StepResult) -> None:
        selector = await self._resolve_selector(action, result)
        await wait_for_element_state(
            self._page, selector, state="visible", timeout=self._wait_timeout,
        )
        await self._page.click(selector)

    async def _do_type(self, action: RecordedAction, result: StepResult) -> None:
        selector = await self._resolve_selector(action, result)
        await wait_for_element_state(
            self._page, selector, state="visible", timeout=self._wait_timeout,
        )
        # Clear the field first.
        await self._page.evaluate(
            f'(() => {{ const el = document.querySelector("{selector}"); '
            f'if (el) {{ el.value = ""; el.dispatchEvent(new Event("input", {{bubbles:true}})); }} }})()'
        )
        await self._page.fill(selector, action.text)

    async def _do_press_key(self, action: RecordedAction, result: StepResult) -> None:
        if action.selector:
            selector = await self._resolve_selector(action, result)
            await self._page.click(selector)
            await asyncio.sleep(0.05)
        await self._page.press_key(action.key)

    async def _do_scroll(self, action: RecordedAction) -> None:
        await self._page.evaluate(
            f"window.scrollTo({action.scroll_x}, {action.scroll_y})"
        )
        await asyncio.sleep(0.2)

    async def _do_select(self, action: RecordedAction, result: StepResult) -> None:
        selector = await self._resolve_selector(action, result)
        await self._page.select(selector, action.text)

    async def _do_check(self, action: RecordedAction, result: StepResult) -> None:
        selector = await self._resolve_selector(action, result)
        if action.text.lower() in ("true", "1", "on"):
            await self._page.check(selector)
        else:
            await self._page.uncheck(selector)

    async def _do_hover(self, action: RecordedAction, result: StepResult) -> None:
        selector = await self._resolve_selector(action, result)
        await self._page.hover(selector)

    # ── self-healing selector resolution ──────────────────────────

    async def _resolve_selector(
        self,
        action: RecordedAction,
        result: StepResult,
    ) -> str:
        """Try the recorded selector; if it fails, attempt to heal it.

        Healing strategies:
          1. Original selector as-is.
          2. If selector contains a class that no longer exists,
             try by text content of the original element.
          3. Try by ``[aria-label]`` matching the recorded text.
          4. Try by ``[placeholder]`` if it was an input.
          5. Try by nearby structure (parent/sibling queries).
        """
        sel = action.selector
        if not sel:
            raise RuntimeError(f"Action {action.kind} has no selector")

        # Strategy 1: original selector.
        if await self._exists(sel):
            return sel

        logger.debug("Selector '%s' not found, attempting heal", sel)

        # Strategy 2: find by text content.
        if action.text:
            healed = await self._find_by_text(action.text, action.kind)
            if healed:
                result.selector_healed = True
                result.healed_selector = healed
                logger.info("Healed selector: '%s' → '%s' (by text)", sel, healed)
                return healed

        # Strategy 3: aria-label.
        if action.text:
            aria_sel = f'[aria-label*="{action.text[:30]}"]'
            if await self._exists(aria_sel):
                result.selector_healed = True
                result.healed_selector = aria_sel
                logger.info("Healed selector: '%s' → '%s' (by aria-label)", sel, aria_sel)
                return aria_sel

        # Strategy 4: try partial class match.
        if "." in sel:
            parts = sel.split(".")
            tag = parts[0] or "*"
            for cls_part in parts[1:]:
                partial = f'{tag}[class*="{cls_part}"]'
                if await self._exists(partial):
                    result.selector_healed = True
                    result.healed_selector = partial
                    logger.info("Healed selector: '%s' → '%s' (partial class)", sel, partial)
                    return partial

        # Strategy 5: fallback to tag + position for inputs.
        if action.kind in ("type", "select", "check"):
            fallback = await self._page.evaluate("""
                (() => {
                    const inputs = document.querySelectorAll('input:not([type="hidden"]), textarea, select');
                    const visible = Array.from(inputs).filter(el => el.offsetParent !== null);
                    if (visible.length === 1) {
                        if (visible[0].id) return '#' + visible[0].id;
                        if (visible[0].name) return '[name="' + visible[0].name + '"]';
                    }
                    return null;
                })()
            """)
            if fallback:
                result.selector_healed = True
                result.healed_selector = fallback
                return fallback

        raise RuntimeError(
            f"Could not heal selector: {sel} (action: {action.kind})"
        )

    async def _exists(self, selector: str) -> bool:
        """Check if a selector matches any element in the DOM."""
        try:
            el = await self._page.query(selector)
            return el is not None
        except Exception:
            return False

    async def _find_by_text(self, text: str, kind: str) -> str | None:
        """Find an element by its text content."""
        escaped = text.replace("'", "\\'")[:50]
        tag_filter = {
            "click": "'a, button, [role=\"button\"], input[type=\"submit\"]'",
            "type":  "'input, textarea'",
            "select": "'select'",
        }.get(kind, "'*'")

        result = await self._page.evaluate(f"""
            (() => {{
                const els = document.querySelectorAll({tag_filter});
                for (const el of els) {{
                    const elText = (el.textContent || el.value || el.placeholder || '').trim().toLowerCase();
                    if (elText.includes('{escaped.lower()}')) {{
                        if (el.id) return '#' + el.id;
                        if (el.dataset && el.dataset.testid) return '[data-testid="' + el.dataset.testid + '"]';
                        if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                        if (el.className && typeof el.className === 'string') {{
                            const cls = el.className.trim().split(/\\s+/)[0];
                            if (cls) return el.tagName.toLowerCase() + '.' + cls;
                        }}
                        return null;
                    }}
                }}
                return null;
            }})()
        """)
        return result if result else None
