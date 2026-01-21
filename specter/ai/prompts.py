"""Prompt templates used by Specter's intelligence layer.

Every prompt is a plain function that accepts structured context and
returns a ready-to-send string.  This keeps prompt logic in one place
and makes it easy to iterate on wording without touching module code.
"""

from __future__ import annotations

import json
from typing import Any


# ── element selection ─────────────────────────────────────────────

def element_selection_prompt(
    description: str,
    dom_snapshot: str,
    *,
    page_url: str = "",
    page_title: str = "",
) -> str:
    """Build a prompt that asks the LLM to find a CSS selector.

    Parameters
    ----------
    description:
        Natural-language description of the target element
        (e.g. "the login button", "email input field").
    dom_snapshot:
        Simplified HTML of the visible DOM (usually the ``<body>``
        subtree with styles/scripts removed).
    page_url:
        Current page URL for extra context.
    page_title:
        Current page title for extra context.
    """
    context_lines = []
    if page_url:
        context_lines.append(f"Page URL: {page_url}")
    if page_title:
        context_lines.append(f"Page title: {page_title}")
    context_block = "\n".join(context_lines) if context_lines else "No additional context."

    return f"""You are an expert at reading HTML and writing CSS selectors.

Given the HTML below, find the element that best matches this description:
"{description}"

{context_block}

HTML (simplified):
```html
{dom_snapshot}
```

Respond ONLY with a JSON object containing:
- "selector": a precise CSS selector that uniquely identifies the element.
- "confidence": a number from 0 to 1 indicating your confidence.
- "reasoning": one sentence explaining your choice.

Do NOT wrap your answer in markdown fences -- respond with raw JSON only."""


def element_selection_system() -> str:
    return (
        "You are a DOM analysis assistant embedded in a browser automation tool. "
        "Your job is to map natural-language element descriptions to precise CSS selectors. "
        "Always prefer selectors based on id, data-testid, aria-label, name, or role attributes "
        "over fragile positional selectors. "
        "Return ONLY valid JSON."
    )


# ── data extraction ───────────────────────────────────────────────

def data_extraction_prompt(
    schema: dict[str, str],
    dom_text: str,
    *,
    page_url: str = "",
    multiple: bool = False,
) -> str:
    """Build a prompt for structured data extraction.

    Parameters
    ----------
    schema:
        Mapping of field name to expected type, e.g.
        ``{"title": "string", "price": "number"}``.
    dom_text:
        Visible text content (or simplified HTML) of the page.
    page_url:
        Current page URL.
    multiple:
        If True, instruct the model to return a list of objects.
    """
    schema_json = json.dumps(schema, indent=2)
    mode = "a JSON array of objects" if multiple else "a single JSON object"
    url_line = f"\nPage URL: {page_url}" if page_url else ""

    return f"""Extract structured data from the text below.

Return {mode} matching this schema:
```json
{schema_json}
```

Rules:
- Use null for any field you cannot confidently extract.
- For "number" fields return a numeric value, not a string.
- For "boolean" fields return true/false.
- Do NOT invent data that is not present in the source text.
{url_line}

Source text:
\"\"\"
{dom_text}
\"\"\"

Respond with raw JSON only -- no commentary, no markdown fences."""


def data_extraction_system() -> str:
    return (
        "You are a structured data extraction engine. "
        "You receive page text and a target schema, and you return clean JSON "
        "that matches the schema. Be precise and conservative -- never hallucinate values."
    )


# ── page description (vision) ────────────────────────────────────

def page_description_prompt(
    question: str = "Describe what is visible on this page.",
) -> str:
    """Prompt sent alongside a screenshot for vision analysis."""
    return f"""{question}

Provide a concise but thorough description covering:
1. The type of page (login form, dashboard, article, e-commerce listing, etc.)
2. Key interactive elements visible (buttons, forms, links)
3. Any important text content (headings, labels, error messages)
4. Layout and visual hierarchy

Respond in plain text, no markdown."""


def page_description_system() -> str:
    return (
        "You are a visual page analysis assistant. "
        "You receive screenshots of web pages and answer questions about their content, "
        "layout, and interactive elements."
    )


def visual_element_find_prompt(description: str) -> str:
    """Ask the vision model to locate an element by visual appearance."""
    return f"""Look at this screenshot and find the element described as:
"{description}"

Return a JSON object with:
- "found": true/false -- whether the element is visible.
- "x": approximate x-coordinate of the element center (pixels from left).
- "y": approximate y-coordinate of the element center (pixels from top).
- "width": approximate element width in pixels.
- "height": approximate element height in pixels.
- "description": one sentence describing what you see at that location.
- "confidence": a number from 0 to 1.

Respond with raw JSON only."""


# ── action planning ───────────────────────────────────────────────

def action_planning_prompt(
    goal: str,
    page_text: str,
    *,
    available_actions: list[str] | None = None,
    page_url: str = "",
) -> str:
    """Ask the LLM to plan a sequence of browser actions.

    Parameters
    ----------
    goal:
        What the user wants to achieve (e.g. "log in with user admin").
    page_text:
        Visible text on the current page.
    available_actions:
        List of action names the agent can perform.
    page_url:
        Current page URL.
    """
    actions_block = ""
    if available_actions:
        actions_block = "Available actions: " + ", ".join(available_actions)
    url_line = f"\nCurrent URL: {page_url}" if page_url else ""

    return f"""You are an intelligent browser automation agent.

Goal: {goal}
{url_line}

Page content (visible text):
\"\"\"
{page_text[:6000]}
\"\"\"

{actions_block}

Plan the minimal sequence of actions needed to achieve the goal.
Return a JSON array of step objects, each with:
- "action": the action to perform (click, type, navigate, scroll, wait, etc.)
- "selector": CSS selector of the target element (if applicable).
- "value": text to type or URL to navigate to (if applicable).
- "description": one sentence explaining this step.

Respond with raw JSON only."""


def action_planning_system() -> str:
    return (
        "You are a browser automation planning agent. "
        "Given a goal and the current page state, you produce a minimal, "
        "ordered sequence of concrete browser actions. "
        "Prefer robust selectors and include wait steps where page loads are expected."
    )
