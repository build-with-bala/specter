"""Smart waiting strategies beyond simple timeouts.

Provides composable wait conditions that can be used anywhere
a ``Page`` operation needs to block until the page reaches a
desired state.

Built-in strategies:
  * **Network idle** -- no pending requests for N seconds.
  * **DOM stable** -- no DOM mutations for N seconds.
  * **Element state** -- wait for visible, hidden, enabled, etc.
  * **URL change** -- wait for the URL to match a pattern.
  * **Custom predicate** -- wait for any JS expression to be truthy.
  * **Composite** -- combine multiple conditions with AND/OR logic.

Usage::

    from specter.automation.waits import (
        wait_for_network_idle, wait_for_dom_stable,
        wait_for_element_state, wait_all, wait_any,
    )

    await wait_for_network_idle(page)
    await wait_for_dom_stable(page, stability_ms=500)
    await wait_for_element_state(page, "#results", state="visible")
    await wait_all(page, [
        lambda p: wait_for_network_idle(p, max_pending=0),
        lambda p: wait_for_element_state(p, ".loaded", state="visible"),
    ])
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from specter.core.page import Page

logger = logging.getLogger(__name__)

# Type alias for a wait condition function.
WaitCondition = Callable[[Page], Awaitable[bool]]


# ── network idle ──────────────────────────────────────────────────

async def wait_for_network_idle(
    page: Page,
    *,
    timeout: float = 30.0,
    idle_time: float = 0.5,
    max_pending: int = 0,
) -> None:
    """Wait until no more than *max_pending* network requests are
    in flight for at least *idle_time* seconds.

    Parameters
    ----------
    page:
        Page to monitor.
    timeout:
        Maximum total wait time.
    idle_time:
        How long the network must be idle before we return.
    max_pending:
        Number of allowed in-flight requests (0 = truly idle,
        2 = tolerate long-polling connections).
    """
    pending = 0
    last_activity = time.monotonic()

    def on_request(_: Any) -> None:
        nonlocal pending, last_activity
        pending += 1
        last_activity = time.monotonic()

    def on_done(_: Any) -> None:
        nonlocal pending, last_activity
        pending = max(0, pending - 1)
        if pending <= max_pending:
            last_activity = time.monotonic()

    page.cdp.on("Network.requestWillBeSent", on_request)
    page.cdp.on("Network.loadingFinished", on_done)
    page.cdp.on("Network.loadingFailed", on_done)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pending <= max_pending and time.monotonic() - last_activity >= idle_time:
                logger.debug("Network idle achieved (pending=%d)", pending)
                return
            await asyncio.sleep(0.1)
        raise TimeoutError(
            f"Network not idle after {timeout}s (still {pending} pending)"
        )
    finally:
        page.cdp.off("Network.requestWillBeSent", on_request)
        page.cdp.off("Network.loadingFinished", on_done)
        page.cdp.off("Network.loadingFailed", on_done)


# ── DOM stable ────────────────────────────────────────────────────

async def wait_for_dom_stable(
    page: Page,
    *,
    timeout: float = 15.0,
    stability_ms: float = 500.0,
    subtree: str | None = None,
) -> None:
    """Wait until the DOM stops mutating.

    Injects a ``MutationObserver`` that tracks the time of the last
    mutation.  Returns once no mutation has occurred for
    *stability_ms* milliseconds.

    Parameters
    ----------
    subtree:
        Optional CSS selector to limit observation to a subtree.
    """
    target = f'document.querySelector("{subtree}")' if subtree else "document.body"
    observer_js = f"""
    new Promise((resolve, reject) => {{
        const target = {target};
        if (!target) {{ resolve(true); return; }}
        let timer = null;
        const observer = new MutationObserver(() => {{
            if (timer) clearTimeout(timer);
            timer = setTimeout(() => {{
                observer.disconnect();
                resolve(true);
            }}, {int(stability_ms)});
        }});
        observer.observe(target, {{
            childList: true,
            subtree: true,
            attributes: true,
            characterData: true,
        }});
        // Start the timer immediately in case no mutations occur.
        timer = setTimeout(() => {{
            observer.disconnect();
            resolve(true);
        }}, {int(stability_ms)});
        // Hard timeout.
        setTimeout(() => {{
            observer.disconnect();
            reject(new Error('DOM stability timeout'));
        }}, {int(timeout * 1000)});
    }})
    """
    try:
        await page.evaluate(observer_js)
        logger.debug("DOM stable")
    except RuntimeError:
        raise TimeoutError(
            f"DOM not stable after {timeout}s"
        )


# ── element state ─────────────────────────────────────────────────

async def wait_for_element_state(
    page: Page,
    selector: str,
    *,
    state: str = "visible",
    timeout: float = 10.0,
) -> None:
    """Wait for an element to reach a specific state.

    Parameters
    ----------
    selector:
        CSS selector for the target element.
    state:
        Desired state -- one of:
        ``"visible"``, ``"hidden"``, ``"attached"``, ``"detached"``,
        ``"enabled"``, ``"disabled"``, ``"checked"``.
    timeout:
        Maximum wait time.
    """
    state = state.lower()
    check_fns: dict[str, str] = {
        "visible": f"""(() => {{
            const el = document.querySelector("{selector}");
            if (!el) return false;
            const s = getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden'
                   && s.opacity !== '0' && el.offsetParent !== null;
        }})()""",
        "hidden": f"""(() => {{
            const el = document.querySelector("{selector}");
            if (!el) return true;
            const s = getComputedStyle(el);
            return s.display === 'none' || s.visibility === 'hidden'
                   || s.opacity === '0' || el.offsetParent === null;
        }})()""",
        "attached": f'!!document.querySelector("{selector}")',
        "detached": f'!document.querySelector("{selector}")',
        "enabled": f'!document.querySelector("{selector}")?.disabled',
        "disabled": f'!!document.querySelector("{selector}")?.disabled',
        "checked": f'!!document.querySelector("{selector}")?.checked',
    }

    expr = check_fns.get(state)
    if not expr:
        raise ValueError(
            f"Unknown state: {state!r}.  "
            f"Must be one of: {', '.join(check_fns.keys())}"
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await page.evaluate(expr)
        if result:
            logger.debug("Element %s is %s", selector, state)
            return
        await asyncio.sleep(0.15)

    raise TimeoutError(
        f"Element {selector!r} did not reach state {state!r} within {timeout}s"
    )


# ── URL change ────────────────────────────────────────────────────

async def wait_for_url(
    page: Page,
    pattern: str,
    *,
    timeout: float = 15.0,
    match_type: str = "contains",
) -> str:
    """Wait until the page URL matches a pattern.

    Parameters
    ----------
    pattern:
        String to match against the URL.
    match_type:
        ``"contains"``, ``"startswith"``, ``"equals"``, or ``"regex"``.

    Returns
    -------
    The matching URL.
    """
    import re as _re

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current_url = await page.evaluate("window.location.href") or ""
        matched = False
        if match_type == "contains":
            matched = pattern in current_url
        elif match_type == "startswith":
            matched = current_url.startswith(pattern)
        elif match_type == "equals":
            matched = current_url == pattern
        elif match_type == "regex":
            matched = bool(_re.search(pattern, current_url))
        else:
            raise ValueError(f"Unknown match_type: {match_type}")

        if matched:
            logger.debug("URL matched: %s", current_url)
            return current_url
        await asyncio.sleep(0.2)

    raise TimeoutError(f"URL did not match {pattern!r} within {timeout}s")


# ── custom predicate ──────────────────────────────────────────────

async def wait_for_predicate(
    page: Page,
    js_expression: str,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.2,
) -> Any:
    """Wait until a JavaScript expression evaluates to a truthy value.

    Parameters
    ----------
    js_expression:
        Any JS expression that can be evaluated in the page context.

    Returns
    -------
    The truthy value returned by the expression.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await page.evaluate(js_expression)
        if value:
            return value
        await asyncio.sleep(poll_interval)
    raise TimeoutError(
        f"Predicate not satisfied within {timeout}s: {js_expression[:80]}"
    )


# ── composite waits ───────────────────────────────────────────────

async def wait_all(
    page: Page,
    conditions: list[Callable[[Page], Awaitable[None]]],
    *,
    timeout: float = 30.0,
) -> None:
    """Wait for ALL conditions to be satisfied (parallel).

    Each condition is an async callable that takes a ``Page`` and
    either returns or raises ``TimeoutError``.
    """
    async def _run(cond: Callable[[Page], Awaitable[None]]) -> None:
        await cond(page)

    try:
        await asyncio.wait_for(
            asyncio.gather(*[_run(c) for c in conditions]),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        raise TimeoutError(f"wait_all: not all conditions met within {timeout}s")


async def wait_any(
    page: Page,
    conditions: list[Callable[[Page], Awaitable[None]]],
    *,
    timeout: float = 30.0,
) -> int:
    """Wait for ANY condition to be satisfied (race).

    Returns the index of the first condition that completed.
    """
    done_idx: int | None = None

    async def _run(idx: int, cond: Callable[[Page], Awaitable[None]]) -> int:
        await cond(page)
        return idx

    tasks = [asyncio.create_task(_run(i, c)) for i, c in enumerate(conditions)]

    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if done:
            result_task = next(iter(done))
            return result_task.result()
        raise TimeoutError(f"wait_any: no condition met within {timeout}s")
    except asyncio.TimeoutError:
        for t in tasks:
            t.cancel()
        raise TimeoutError(f"wait_any: no condition met within {timeout}s")
