"""High-level action primitives that compose Page methods.

These are the building blocks a human operator or an AI agent would
reach for: ``login()``, ``fill_form()``, ``extract_table()``, etc.
Each action handles the full workflow including waiting, scrolling,
error handling, and retries.

Usage::

    from specter.automation.actions import Actions

    actions = Actions(page)
    await actions.login("https://app.example.com/login",
                        username="admin", password="secret")
    data = await actions.extract_table("table.results")
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from specter.core.page import Page
from specter.core.types import ElementHandle
from specter.automation.waits import (
    wait_for_network_idle,
    wait_for_dom_stable,
    wait_for_element_state,
    wait_for_url,
)

logger = logging.getLogger(__name__)


class Actions:
    """High-level action primitives built on top of ``Page``.

    Parameters
    ----------
    page:
        The ``Page`` instance to act on.
    default_timeout:
        Default timeout for all wait operations.
    retry_count:
        Default number of retries for flaky actions.
    """

    def __init__(
        self,
        page: Page,
        *,
        default_timeout: float = 15.0,
        retry_count: int = 2,
    ):
        self._page = page
        self._timeout = default_timeout
        self._retries = retry_count

    # ── login ─────────────────────────────────────────────────────

    async def login(
        self,
        url: str,
        *,
        username: str,
        password: str,
        username_selector: str = "",
        password_selector: str = "",
        submit_selector: str = "",
        wait_after: str = "",
    ) -> bool:
        """Log in to a website.

        Auto-detects username, password, and submit elements if
        selectors are not provided.

        Parameters
        ----------
        url:
            Login page URL.
        username / password:
            Credentials to fill.
        username_selector:
            CSS selector for the username field.  If empty, auto-detect.
        password_selector:
            CSS selector for the password field.  If empty, auto-detect.
        submit_selector:
            CSS selector for the submit button.  If empty, auto-detect.
        wait_after:
            Optional CSS selector or URL substring to confirm
            successful login.

        Returns
        -------
        ``True`` if the login appears successful.
        """
        await self._page.goto(url)
        await wait_for_dom_stable(self._page, stability_ms=300)

        # Auto-detect selectors.
        if not username_selector:
            username_selector = await self._find_input_selector(
                ["email", "username", "user", "login", "mail"],
                input_type=["text", "email"],
            )
        if not password_selector:
            password_selector = await self._find_input_selector(
                ["password", "pass", "pwd"],
                input_type=["password"],
            )
        if not submit_selector:
            submit_selector = await self._find_submit_selector()

        if not username_selector or not password_selector:
            raise RuntimeError("Could not auto-detect login form fields")

        logger.info("Login: user=%s pass=%s submit=%s",
                     username_selector, password_selector, submit_selector)

        # Fill credentials.
        await self._page.click(username_selector)
        await asyncio.sleep(0.1)
        await self._page.fill(username_selector, username)

        await self._page.click(password_selector)
        await asyncio.sleep(0.1)
        await self._page.fill(password_selector, password)

        # Submit.
        if submit_selector:
            await self._page.click(submit_selector)
        else:
            await self._page.press_key("Enter")

        # Wait for navigation / result.
        try:
            await wait_for_network_idle(self._page, timeout=self._timeout, idle_time=1.0)
        except TimeoutError:
            pass

        if wait_after:
            try:
                if wait_after.startswith(("http", "/")):
                    await wait_for_url(self._page, wait_after, timeout=self._timeout)
                else:
                    await wait_for_element_state(
                        self._page, wait_after, state="visible", timeout=self._timeout,
                    )
                return True
            except TimeoutError:
                return False

        # Heuristic: if URL changed from the login page, probably success.
        current_url = await self._page.evaluate("window.location.href") or ""
        return current_url != url

    # ── form filling ──────────────────────────────────────────────

    async def fill_form(
        self,
        data: dict[str, str],
        *,
        form_selector: str = "form",
        submit: bool = True,
    ) -> None:
        """Fill a form with the given field data.

        Parameters
        ----------
        data:
            Mapping of ``field_name_or_selector → value``.
            Keys are tried as: CSS selector first, then ``[name=...]``,
            then ``[placeholder*=...]``, then ``[aria-label*=...]``.
        form_selector:
            CSS selector of the containing form.
        submit:
            If ``True``, click the submit button after filling.
        """
        for field, value in data.items():
            sel = await self._resolve_field_selector(field)
            if not sel:
                logger.warning("Could not find field: %s", field)
                continue

            # Determine field type.
            field_type = await self._page.evaluate(
                f'document.querySelector("{sel}")?.type?.toLowerCase()'
            ) or ""
            tag = await self._page.evaluate(
                f'document.querySelector("{sel}")?.tagName?.toLowerCase()'
            ) or ""

            if tag == "select":
                await self._page.select(sel, value)
            elif field_type == "checkbox":
                if value.lower() in ("true", "yes", "1", "on"):
                    await self._page.check(sel)
                else:
                    await self._page.uncheck(sel)
            elif field_type == "radio":
                await self._page.click(f'{sel}[value="{value}"]')
            elif field_type == "file":
                # File inputs need special handling via CDP.
                await self._page.cdp.send("DOM.setFileInputFiles", {
                    "files": [value],
                    "selector": sel,
                })
            else:
                # Clear and fill text input.
                await self._page.click(sel)
                await self._page.evaluate(
                    f'document.querySelector("{sel}").value = ""'
                )
                await self._page.fill(sel, value)

            await asyncio.sleep(0.05)

        if submit:
            submit_sel = await self._find_submit_in_form(form_selector)
            if submit_sel:
                await self._page.click(submit_sel)
            else:
                await self._page.press_key("Enter")

    # ── table extraction ──────────────────────────────────────────

    async def extract_table(
        self,
        selector: str = "table",
    ) -> list[dict[str, str]]:
        """Extract data from an HTML table as a list of dicts.

        The first row (or ``<thead>``) is used as column headers.

        Parameters
        ----------
        selector:
            CSS selector for the table element.

        Returns
        -------
        List of row dicts, each keyed by the header text.
        """
        js = f"""
        (() => {{
            const table = document.querySelector('{selector}');
            if (!table) return null;

            const headers = [];
            const headerCells = table.querySelectorAll(
                'thead th, thead td, tr:first-child th, tr:first-child td'
            );
            headerCells.forEach(th => headers.push(th.textContent.trim()));
            if (!headers.length) return null;

            const rows = [];
            const bodyRows = table.querySelectorAll('tbody tr');
            const allRows = bodyRows.length ? bodyRows : table.querySelectorAll('tr');
            allRows.forEach((tr, i) => {{
                const cells = tr.querySelectorAll('td');
                if (cells.length === 0) return;
                const row = {{}};
                cells.forEach((td, j) => {{
                    if (j < headers.length) {{
                        row[headers[j]] = td.textContent.trim();
                    }}
                }});
                rows.push(row);
            }});
            return rows;
        }})()
        """
        result = await self._page.evaluate(js)
        return result or []

    # ── link extraction ───────────────────────────────────────────

    async def extract_links(
        self,
        *,
        selector: str = "a[href]",
        absolute: bool = True,
    ) -> list[dict[str, str]]:
        """Extract all links from the page.

        Returns
        -------
        List of dicts with ``"text"``, ``"href"``, and ``"title"`` keys.
        """
        base_url = await self._page.evaluate("window.location.origin") or ""
        js = f"""
        (() => {{
            const links = [];
            document.querySelectorAll('{selector}').forEach(a => {{
                let href = a.getAttribute('href') || '';
                if ({str(absolute).lower()} && href && !href.startsWith('http') && !href.startsWith('//')) {{
                    href = '{base_url}' + (href.startsWith('/') ? '' : '/') + href;
                }}
                links.push({{
                    text: a.textContent.trim().substring(0, 200),
                    href: href,
                    title: a.getAttribute('title') || '',
                }});
            }});
            return links;
        }})()
        """
        return await self._page.evaluate(js) or []

    # ── text extraction ───────────────────────────────────────────

    async def extract_text(
        self,
        selector: str = "body",
        *,
        clean: bool = True,
    ) -> str:
        """Extract text content from an element.

        Parameters
        ----------
        clean:
            If ``True``, collapse whitespace and strip.
        """
        text = await self._page.evaluate(
            f'document.querySelector("{selector}")?.innerText || ""'
        ) or ""
        if clean:
            import re
            text = re.sub(r"\s+", " ", text).strip()
        return text

    # ── screenshot with annotation ────────────────────────────────

    async def screenshot_element(
        self,
        selector: str,
        path: str,
    ) -> bytes:
        """Take a screenshot of a specific element."""
        el = await self._page.wait_for(selector, timeout=self._timeout)
        assert el.box, f"Element {selector} has no layout box"
        b = el.box
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

    # ── retry wrapper ─────────────────────────────────────────────

    async def retry(
        self,
        action: Any,
        *args: Any,
        retries: int | None = None,
        delay: float = 1.0,
        **kwargs: Any,
    ) -> Any:
        """Execute an async action with retries on failure.

        Parameters
        ----------
        action:
            Async callable to retry.
        retries:
            Number of retries (defaults to instance default).
        delay:
            Delay between retries in seconds.
        """
        max_attempts = (retries or self._retries) + 1
        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                return await action(*args, **kwargs)
            except Exception as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    logger.warning("Retry %d/%d: %s", attempt + 1, max_attempts - 1, e)
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    # ── internals ─────────────────────────────────────────────────

    async def _find_input_selector(
        self,
        name_hints: list[str],
        input_type: list[str] | None = None,
    ) -> str:
        """Find an input element by name/id/placeholder heuristics."""
        type_filter = ""
        if input_type:
            conditions = " || ".join(
                f'inp.type === "{t}"' for t in input_type
            )
            type_filter = f"if (!({conditions})) continue;"

        hints_json = str(name_hints).replace("'", '"')
        js = f"""
        (() => {{
            const hints = {hints_json};
            const inputs = document.querySelectorAll('input, textarea');
            for (const inp of inputs) {{
                {type_filter}
                const name = (inp.name || '').toLowerCase();
                const id = (inp.id || '').toLowerCase();
                const ph = (inp.placeholder || '').toLowerCase();
                const label = (inp.getAttribute('aria-label') || '').toLowerCase();
                const auto = (inp.getAttribute('autocomplete') || '').toLowerCase();
                for (const hint of hints) {{
                    if (name.includes(hint) || id.includes(hint) || ph.includes(hint)
                        || label.includes(hint) || auto.includes(hint)) {{
                        if (inp.id) return '#' + inp.id;
                        if (inp.name) return 'input[name="' + inp.name + '"]';
                        return null;
                    }}
                }}
            }}
            return null;
        }})()
        """
        return await self._page.evaluate(js) or ""

    async def _find_submit_selector(self) -> str:
        """Find a submit button."""
        js = """
        (() => {
            // Explicit submit buttons.
            const submit = document.querySelector(
                'button[type="submit"], input[type="submit"]'
            );
            if (submit) {
                if (submit.id) return '#' + submit.id;
                if (submit.name) return '[name="' + submit.name + '"]';
                return submit.tagName.toLowerCase() + '[type="submit"]';
            }
            // Buttons with login-related text.
            const buttons = document.querySelectorAll('button, [role="button"]');
            for (const btn of buttons) {
                const text = btn.textContent.toLowerCase().trim();
                if (['log in', 'login', 'sign in', 'signin', 'submit', 'enter'].some(
                    k => text.includes(k)
                )) {
                    if (btn.id) return '#' + btn.id;
                    return null;
                }
            }
            return null;
        })()
        """
        return await self._page.evaluate(js) or ""

    async def _find_submit_in_form(self, form_selector: str) -> str:
        """Find a submit button inside a specific form."""
        js = f"""
        (() => {{
            const form = document.querySelector('{form_selector}');
            if (!form) return null;
            const submit = form.querySelector(
                'button[type="submit"], input[type="submit"], button:not([type])'
            );
            if (!submit) return null;
            if (submit.id) return '#' + submit.id;
            if (submit.name) return '{form_selector} [name="' + submit.name + '"]';
            return '{form_selector} ' + submit.tagName.toLowerCase() +
                   (submit.type ? '[type="' + submit.type + '"]' : '');
        }})()
        """
        return await self._page.evaluate(js) or ""

    async def _resolve_field_selector(self, field: str) -> str:
        """Resolve a field name/key to a CSS selector.

        Tries, in order:
          1. As a direct CSS selector.
          2. ``[name="field"]``
          3. ``[id="field"]``
          4. ``[placeholder*="field"]``
          5. ``[aria-label*="field"]``
        """
        # Direct selector.
        try:
            el = await self._page.query(field)
            if el:
                return field
        except Exception:
            pass

        # Name attribute.
        sel = f'[name="{field}"]'
        el = await self._page.query(sel)
        if el:
            return sel

        # ID.
        sel = f'#{field}'
        el = await self._page.query(sel)
        if el:
            return sel

        # Placeholder (case insensitive via JS).
        found = await self._page.evaluate(f"""
            (() => {{
                const el = document.querySelector('[placeholder]');
                const all = document.querySelectorAll('input, textarea, select');
                for (const inp of all) {{
                    const ph = (inp.placeholder || '').toLowerCase();
                    const nm = (inp.name || '').toLowerCase();
                    const lb = (inp.getAttribute('aria-label') || '').toLowerCase();
                    const target = '{field.lower()}';
                    if (ph.includes(target) || nm.includes(target) || lb.includes(target)) {{
                        if (inp.id) return '#' + inp.id;
                        if (inp.name) return '[name="' + inp.name + '"]';
                    }}
                }}
                return null;
            }})()
        """)
        return found or ""
