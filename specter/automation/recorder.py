"""Record user actions by listening to CDP input events and DOM mutations.

Captures clicks, keystrokes, navigation, and scroll events, then
serialises them as a list of ``RecordedAction`` objects that can be
saved to JSON and later replayed by ``specter.automation.replayer``.

The recorder injects a thin JS layer via ``Page.addScriptToEvaluateOnNewDocument``
that observes DOM events and pushes structured messages back to
Python through ``Runtime.bindingCalled``.

Usage::

    from specter.automation.recorder import ActionRecorder

    recorder = ActionRecorder(page)
    await recorder.start()
    # ... user interacts with the page ...
    await recorder.stop()
    recorder.save("session.json")
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from specter.core.page import Page
from specter.core.types import RecordedAction

logger = logging.getLogger(__name__)

# Name of the JS binding function used to send events to Python.
_BINDING_NAME = "__specterRecordEvent"

# JS injected into the page to capture user actions.
_RECORDER_SCRIPT = """
(function() {
    if (window.__specterRecorderActive) return;
    window.__specterRecorderActive = true;

    function getSelector(el) {
        if (!el || el === document || el === document.body) return 'body';
        if (el.id) return '#' + el.id;
        if (el.dataset && el.dataset.testid) return '[data-testid="' + el.dataset.testid + '"]';
        if (el.getAttribute && el.getAttribute('name'))
            return el.tagName.toLowerCase() + '[name="' + el.getAttribute('name') + '"]';
        if (el.getAttribute && el.getAttribute('aria-label'))
            return '[aria-label="' + el.getAttribute('aria-label') + '"]';
        if (el.className && typeof el.className === 'string') {
            const cls = el.className.trim().split(/\\s+/).slice(0, 2).join('.');
            if (cls) return el.tagName.toLowerCase() + '.' + cls;
        }
        // Positional fallback.
        const parent = el.parentElement;
        if (parent) {
            const siblings = Array.from(parent.children).filter(c => c.tagName === el.tagName);
            if (siblings.length > 1) {
                const idx = siblings.indexOf(el) + 1;
                return getSelector(parent) + ' > ' + el.tagName.toLowerCase() + ':nth-child(' + idx + ')';
            }
        }
        return el.tagName.toLowerCase();
    }

    function send(kind, detail) {
        detail.kind = kind;
        detail.timestamp = Date.now();
        detail.url = window.location.href;
        try {
            window.__specterRecordEvent(JSON.stringify(detail));
        } catch(e) {}
    }

    // Click.
    document.addEventListener('click', function(e) {
        const sel = getSelector(e.target);
        send('click', {
            selector: sel,
            x: e.clientX,
            y: e.clientY,
            text: (e.target.textContent || '').trim().substring(0, 100),
        });
    }, true);

    // Input (typing).
    document.addEventListener('input', function(e) {
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA'
            || e.target.isContentEditable) {
            const sel = getSelector(e.target);
            send('type', {
                selector: sel,
                text: e.target.value || e.target.textContent || '',
            });
        }
    }, true);

    // Change (selects, checkboxes).
    document.addEventListener('change', function(e) {
        const sel = getSelector(e.target);
        const tag = e.target.tagName.toLowerCase();
        if (tag === 'select') {
            send('select', { selector: sel, text: e.target.value });
        } else if (e.target.type === 'checkbox') {
            send('check', { selector: sel, text: String(e.target.checked) });
        } else if (e.target.type === 'radio') {
            send('select', { selector: sel, text: e.target.value });
        }
    }, true);

    // Keyboard.
    document.addEventListener('keydown', function(e) {
        if (['Enter', 'Tab', 'Escape', 'Backspace', 'Delete',
             'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {
            const sel = getSelector(e.target);
            send('press_key', { selector: sel, key: e.key });
        }
    }, true);

    // Scroll.
    let scrollTimer = null;
    window.addEventListener('scroll', function() {
        if (scrollTimer) clearTimeout(scrollTimer);
        scrollTimer = setTimeout(function() {
            send('scroll', {
                scroll_x: window.scrollX,
                scroll_y: window.scrollY,
            });
        }, 200);
    }, true);

    // Before unload (navigation).
    window.addEventListener('beforeunload', function() {
        send('navigate', { url: window.location.href });
    });

    console.log('[Specter] Recorder attached');
})();
"""


class ActionRecorder:
    """Records browser interactions for later replay.

    Parameters
    ----------
    page:
        The ``Page`` to record.
    """

    def __init__(self, page: Page):
        self._page = page
        self._actions: list[RecordedAction] = []
        self._recording = False
        self._start_time: float = 0.0
        self._last_type_action: RecordedAction | None = None
        self._debounce_ms: float = 300.0

    @property
    def actions(self) -> list[RecordedAction]:
        """Return a copy of the recorded actions."""
        return list(self._actions)

    @property
    def is_recording(self) -> bool:
        return self._recording

    # ── lifecycle ─────────────────────────────────────────────────

    async def start(self) -> None:
        """Begin recording user actions."""
        if self._recording:
            return

        # Add a JS binding so the page can send events to Python.
        await self._page.cdp.send("Runtime.addBinding", {"name": _BINDING_NAME})
        self._page.cdp.on("Runtime.bindingCalled", self._on_binding)

        # Inject the recorder script on every new document.
        await self._page.cdp.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": _RECORDER_SCRIPT,
        })
        # Also inject into the current page immediately.
        await self._page.evaluate(_RECORDER_SCRIPT)

        # Listen for navigation events.
        self._page.cdp.on("Page.frameNavigated", self._on_navigate)

        self._recording = True
        self._start_time = time.time()
        self._actions.clear()
        logger.info("Recording started")

    async def stop(self) -> None:
        """Stop recording."""
        if not self._recording:
            return
        self._page.cdp.off("Runtime.bindingCalled", self._on_binding)
        self._page.cdp.off("Page.frameNavigated", self._on_navigate)
        self._recording = False
        self._flush_type_buffer()
        logger.info("Recording stopped: %d actions captured", len(self._actions))

    # ── persistence ───────────────────────────────────────────────

    def save(self, path: str) -> None:
        """Save recorded actions to a JSON file.

        Parameters
        ----------
        path:
            File path to write.  Parent directories are created
            automatically.
        """
        self._flush_type_buffer()
        out = []
        for a in self._actions:
            item: dict[str, Any] = {"kind": a.kind, "timestamp": a.timestamp}
            if a.selector:
                item["selector"] = a.selector
            if a.url:
                item["url"] = a.url
            if a.text:
                item["text"] = a.text
            if a.key:
                item["key"] = a.key
            if a.x or a.y:
                item["x"] = a.x
                item["y"] = a.y
            if a.scroll_x or a.scroll_y:
                item["scroll_x"] = a.scroll_x
                item["scroll_y"] = a.scroll_y
            if a.meta:
                item["meta"] = a.meta
            out.append(item)

        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2))
        logger.info("Saved %d actions to %s", len(out), path)

    @classmethod
    def load(cls, path: str) -> list[RecordedAction]:
        """Load actions from a JSON file.

        Returns
        -------
        List of ``RecordedAction`` objects.
        """
        data = json.loads(Path(path).read_text())
        actions: list[RecordedAction] = []
        for item in data:
            actions.append(RecordedAction(
                kind=item.get("kind", ""),
                timestamp=item.get("timestamp", 0),
                selector=item.get("selector", ""),
                url=item.get("url", ""),
                text=item.get("text", ""),
                key=item.get("key", ""),
                x=item.get("x", 0.0),
                y=item.get("y", 0.0),
                scroll_x=item.get("scroll_x", 0),
                scroll_y=item.get("scroll_y", 0),
                meta=item.get("meta", {}),
            ))
        return actions

    # ── internal handlers ─────────────────────────────────────────

    def _on_binding(self, params: dict) -> None:
        """Handle Runtime.bindingCalled events from the recorder script."""
        if params.get("name") != _BINDING_NAME:
            return
        payload_str = params.get("payload", "")
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning("Invalid recorder payload: %s", payload_str[:200])
            return

        kind = payload.get("kind", "")
        ts = payload.get("timestamp", time.time() * 1000) / 1000.0  # ms -> s

        # Debounce rapid type events (coalesce into one action).
        if kind == "type":
            self._buffer_type_action(payload, ts)
            return

        # Flush any pending type action.
        self._flush_type_buffer()

        action = RecordedAction(
            kind=kind,
            timestamp=ts,
            selector=payload.get("selector", ""),
            url=payload.get("url", ""),
            text=payload.get("text", ""),
            key=payload.get("key", ""),
            x=payload.get("x", 0.0),
            y=payload.get("y", 0.0),
            scroll_x=payload.get("scroll_x", 0),
            scroll_y=payload.get("scroll_y", 0),
        )
        self._actions.append(action)
        logger.debug("Recorded: %s %s", kind, action.selector or action.url or "")

    def _on_navigate(self, params: dict) -> None:
        """Record page navigations."""
        frame = params.get("frame", {})
        if frame.get("parentId"):
            return  # Skip iframes.
        url = frame.get("url", "")
        if url and url != "about:blank":
            self._flush_type_buffer()
            self._actions.append(RecordedAction(
                kind="navigate",
                timestamp=time.time(),
                url=url,
            ))
            logger.debug("Recorded: navigate %s", url)

    def _buffer_type_action(self, payload: dict, ts: float) -> None:
        """Buffer type actions to coalesce rapid keystrokes."""
        selector = payload.get("selector", "")
        text = payload.get("text", "")

        if (self._last_type_action
            and self._last_type_action.selector == selector
            and ts - self._last_type_action.timestamp < self._debounce_ms / 1000.0):
            # Update the existing buffered action.
            self._last_type_action.text = text
            self._last_type_action.timestamp = ts
        else:
            self._flush_type_buffer()
            self._last_type_action = RecordedAction(
                kind="type",
                timestamp=ts,
                selector=selector,
                text=text,
            )

    def _flush_type_buffer(self) -> None:
        """Emit the buffered type action."""
        if self._last_type_action:
            self._actions.append(self._last_type_action)
            logger.debug("Recorded: type %s → '%s'",
                         self._last_type_action.selector,
                         self._last_type_action.text[:50])
            self._last_type_action = None
