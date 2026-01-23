"""AI-powered element selection from natural-language descriptions.

Instead of writing fragile CSS selectors by hand, describe the
element you want and let an LLM find it by analysing the page
structure.

Usage::

    from specter.intelligence.selector import SmartSelector
    sel = SmartSelector(page, ai_provider)
    result = await sel.find("the login button")
    await page.click(result.selector)

    # Or find multiple candidates:
    results = await sel.find_all("navigation links")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from specter.core.page import Page
from specter.ai.provider import AIProvider
from specter.ai.prompts import element_selection_prompt, element_selection_system

logger = logging.getLogger(__name__)

# Maximum DOM size (chars) to send to the LLM.
_MAX_DOM_SIZE = 30_000


# ── result type ───────────────────────────────────────────────────

@dataclass
class SelectorResult:
    """Result of an AI-powered element search."""
    selector: str
    confidence: float    # 0.0 .. 1.0
    reasoning: str
    verified: bool       # True if the selector was confirmed to exist in the DOM


# ── DOM simplification ────────────────────────────────────────────

def _simplify_dom(html: str) -> str:
    """Strip scripts, styles, SVGs, and excessive whitespace.

    The goal is to produce a compact but readable HTML representation
    that retains all interactive elements, labels, and attributes
    relevant to selector generation.
    """
    # Remove script / style / svg blocks.
    for tag in ("script", "style", "svg", "noscript", "link", "meta"):
        html = re.sub(
            rf"<{tag}[\s>].*?</{tag}>",
            "",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Self-closing.
        html = re.sub(rf"<{tag}\b[^>]*/?>", "", html, flags=re.IGNORECASE)

    # Remove comments.
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)

    # Remove data URIs (huge base64 blobs).
    html = re.sub(r'data:[^"\']*["\']', '""', html)

    # Collapse whitespace.
    html = re.sub(r"\s{2,}", " ", html)
    html = re.sub(r">\s+<", ">\n<", html)

    return html.strip()


async def _get_page_dom(page: Page) -> str:
    """Extract a simplified DOM snapshot from the page."""
    raw = await page.evaluate("""
        (() => {
            const clone = document.body.cloneNode(true);
            // Remove hidden elements.
            clone.querySelectorAll('[hidden], [style*="display:none"], [style*="display: none"]')
                .forEach(el => el.remove());
            return clone.outerHTML;
        })()
    """)
    if not raw:
        raw = await page.content()
    simplified = _simplify_dom(raw or "")
    if len(simplified) > _MAX_DOM_SIZE:
        simplified = simplified[:_MAX_DOM_SIZE] + "\n<!-- truncated -->"
    return simplified


# ── selector engine ───────────────────────────────────────────────

class SmartSelector:
    """Find elements by natural-language description using an LLM.

    Parameters
    ----------
    page:
        The ``Page`` instance to search within.
    provider:
        An ``AIProvider`` for LLM calls.
    """

    def __init__(self, page: Page, provider: AIProvider):
        self._page = page
        self._ai = provider

    async def find(
        self,
        description: str,
        *,
        verify: bool = True,
        fallback_strategies: bool = True,
    ) -> SelectorResult:
        """Find a single element matching *description*.

        Parameters
        ----------
        description:
            Natural-language description (e.g. "the search input",
            "Sign Up button", "first product price").
        verify:
            If ``True``, confirm the returned selector exists in
            the live DOM before returning.
        fallback_strategies:
            If ``True`` and the LLM selector doesn't verify,
            try heuristic fallback methods.

        Raises
        ------
        RuntimeError
            If no matching element can be found.
        """
        dom = await _get_page_dom(self._page)
        page_url = self._page.url
        page_title = await self._page.title()

        prompt = element_selection_prompt(
            description, dom,
            page_url=page_url, page_title=page_title,
        )
        system = element_selection_system()

        data = await self._ai.generate_json(prompt, system=system)
        selector = data.get("selector", "")
        confidence = float(data.get("confidence", 0))
        reasoning = data.get("reasoning", "")

        if not selector:
            if fallback_strategies:
                return await self._heuristic_find(description)
            raise RuntimeError(f"LLM returned no selector for: {description}")

        result = SelectorResult(
            selector=selector,
            confidence=confidence,
            reasoning=reasoning,
            verified=False,
        )

        if verify:
            result.verified = await self._verify(selector)
            if not result.verified and fallback_strategies:
                logger.info("LLM selector '%s' not found, trying heuristics", selector)
                return await self._heuristic_find(description)

        logger.info("SmartSelector: '%s' → %s (confidence=%.2f, verified=%s)",
                     description, selector, confidence, result.verified)
        return result

    async def find_all(
        self,
        description: str,
        *,
        max_results: int = 10,
    ) -> list[SelectorResult]:
        """Find multiple elements matching *description*.

        Returns up to *max_results* results, each with a separate
        selector.
        """
        dom = await _get_page_dom(self._page)
        page_url = self._page.url
        page_title = await self._page.title()

        prompt = element_selection_prompt(
            description, dom,
            page_url=page_url, page_title=page_title,
        )
        # Override the instruction to request multiple results.
        prompt += (
            f"\n\nActually, find up to {max_results} elements matching the description. "
            "Return a JSON object with a \"results\" array, each item having "
            "\"selector\", \"confidence\", and \"reasoning\"."
        )
        system = element_selection_system()
        data = await self._ai.generate_json(prompt, system=system)

        results_raw = data.get("results", [])
        if not results_raw and data.get("selector"):
            results_raw = [data]

        results: list[SelectorResult] = []
        for item in results_raw[:max_results]:
            sel = item.get("selector", "")
            if sel:
                verified = await self._verify(sel)
                results.append(SelectorResult(
                    selector=sel,
                    confidence=float(item.get("confidence", 0)),
                    reasoning=item.get("reasoning", ""),
                    verified=verified,
                ))
        return results

    # ── heuristic fallback ────────────────────────────────────────

    async def _heuristic_find(self, description: str) -> SelectorResult:
        """Try rule-based strategies when the LLM fails.

        Strategies (in priority order):
          1. Search by aria-label.
          2. Search by text content (buttons, links).
          3. Search by placeholder.
          4. Search by name attribute.
          5. Search by title attribute.
        """
        desc_lower = description.lower().strip()

        strategies: list[tuple[str, str]] = [
            # aria-label
            (f'[aria-label="{description}" i]', "aria-label match"),
            (f'[aria-label*="{desc_lower}" i]', "partial aria-label match"),
            # Buttons / links by text (via XPath workaround expressed as JS).
            ("__text_match__", "text content match"),
            # Placeholder
            (f'[placeholder*="{desc_lower}" i]', "placeholder match"),
            # Name
            (f'[name*="{desc_lower}" i]', "name attribute match"),
            # Title
            (f'[title*="{desc_lower}" i]', "title attribute match"),
            # data-testid
            (f'[data-testid*="{desc_lower}" i]', "data-testid match"),
        ]

        for css, reason in strategies:
            if css == "__text_match__":
                # Use JS to find by visible text.
                found = await self._find_by_text(desc_lower)
                if found:
                    return SelectorResult(
                        selector=found,
                        confidence=0.6,
                        reasoning=f"Heuristic: {reason}",
                        verified=True,
                    )
                continue

            if await self._verify(css):
                return SelectorResult(
                    selector=css,
                    confidence=0.5,
                    reasoning=f"Heuristic: {reason}",
                    verified=True,
                )

        raise RuntimeError(
            f"Could not find any element matching: {description}"
        )

    async def _find_by_text(self, text: str) -> str | None:
        """Find a clickable element containing the given text."""
        escaped = text.replace("'", "\\'")
        selector = await self._page.evaluate(f"""
            (() => {{
                const targets = document.querySelectorAll(
                    'a, button, [role="button"], [role="link"], input[type="submit"], input[type="button"]'
                );
                for (const el of targets) {{
                    const elText = (el.textContent || el.value || '').trim().toLowerCase();
                    if (elText.includes('{escaped}')) {{
                        if (el.id) return '#' + el.id;
                        if (el.dataset.testid) return '[data-testid="' + el.dataset.testid + '"]';
                        if (el.name) return el.tagName.toLowerCase() + '[name="' + el.name + '"]';
                        if (el.className) return el.tagName.toLowerCase() + '.' + el.className.split(' ')[0];
                        return el.tagName.toLowerCase();
                    }}
                }}
                return null;
            }})()
        """)
        return selector if selector else None

    # ── verification ──────────────────────────────────────────────

    async def _verify(self, selector: str) -> bool:
        """Check whether a selector matches at least one element."""
        try:
            el = await self._page.query(selector)
            return el is not None
        except Exception:
            return False
