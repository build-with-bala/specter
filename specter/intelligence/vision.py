"""Visual page understanding using screenshots + vision LLM.

Captures a screenshot via CDP and sends it to a vision-capable model
(GPT-4o, Llama 3.2 Vision via Ollama, etc.) to:

  * **Describe** what is visible on the page.
  * **Find elements** by visual appearance / description.
  * **Answer questions** about the page content and layout.
  * **Compare** screenshots to detect changes.

Usage::

    from specter.intelligence.vision import VisionAnalyzer

    vision = VisionAnalyzer(page, ai_provider)
    desc = await vision.describe_page()
    loc  = await vision.find_element("the red checkout button")
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from specter.core.page import Page
from specter.ai.provider import AIProvider
from specter.ai.prompts import (
    page_description_prompt,
    page_description_system,
    visual_element_find_prompt,
)

logger = logging.getLogger(__name__)


# ── result types ──────────────────────────────────────────────────

@dataclass
class PageDescription:
    """Result of a page description request."""
    text: str
    page_type: str       # extracted from the description
    key_elements: list[str]

    @classmethod
    def from_text(cls, text: str) -> "PageDescription":
        """Parse a free-text description into structured fields."""
        lines = text.strip().split("\n")
        page_type = ""
        elements: list[str] = []
        for line in lines:
            line_lower = line.strip().lower()
            if "type" in line_lower and ":" in line:
                page_type = line.split(":", 1)[1].strip()
            elif any(kw in line_lower for kw in ("button", "input", "link", "form",
                                                  "menu", "nav", "header", "footer")):
                elements.append(line.strip().lstrip("- "))
        return cls(text=text, page_type=page_type, key_elements=elements)


@dataclass
class VisualElementLocation:
    """Location of an element found by visual analysis."""
    found: bool
    x: float              # center x (pixels)
    y: float              # center y (pixels)
    width: float
    height: float
    description: str
    confidence: float     # 0.0 .. 1.0


# ── vision analyzer ──────────────────────────────────────────────

class VisionAnalyzer:
    """Visual page analysis powered by screenshot + vision LLM.

    Parameters
    ----------
    page:
        The ``Page`` to analyse (used for taking screenshots).
    provider:
        An ``AIProvider`` configured with a vision-capable model.
    """

    def __init__(self, page: Page, provider: AIProvider):
        self._page = page
        self._ai = provider

    async def describe_page(
        self,
        *,
        question: str = "Describe what is visible on this page.",
        full_page: bool = False,
    ) -> PageDescription:
        """Take a screenshot and describe the page content.

        Parameters
        ----------
        question:
            Custom question to ask about the page.
        full_page:
            If ``True``, capture the full scrollable page instead of
            just the viewport.
        """
        screenshot_b64 = await self._capture_b64(full_page=full_page)
        prompt = page_description_prompt(question)
        system = page_description_system()

        # Build a vision message with text + image.
        raw = await self._vision_request(prompt, screenshot_b64, system=system)
        return PageDescription.from_text(raw)

    async def find_element(
        self,
        description: str,
        *,
        full_page: bool = False,
    ) -> VisualElementLocation:
        """Find an element by visual description.

        Parameters
        ----------
        description:
            Natural-language description of the element's visual
            appearance (e.g. "the blue Add to Cart button",
            "the profile picture in the top right").
        """
        screenshot_b64 = await self._capture_b64(full_page=full_page)
        prompt = visual_element_find_prompt(description)

        data = await self._vision_json_request(prompt, screenshot_b64)

        return VisualElementLocation(
            found=data.get("found", False),
            x=float(data.get("x", 0)),
            y=float(data.get("y", 0)),
            width=float(data.get("width", 0)),
            height=float(data.get("height", 0)),
            description=data.get("description", ""),
            confidence=float(data.get("confidence", 0)),
        )

    async def ask(
        self,
        question: str,
        *,
        full_page: bool = False,
    ) -> str:
        """Ask an arbitrary question about the page's visual content.

        Parameters
        ----------
        question:
            Free-form question (e.g. "Is there an error message?",
            "What colour is the primary button?").
        """
        screenshot_b64 = await self._capture_b64(full_page=full_page)
        return await self._vision_request(
            question,
            screenshot_b64,
            system=page_description_system(),
        )

    async def compare(
        self,
        screenshot_before: bytes,
        *,
        question: str = "What changed between these two screenshots?",
    ) -> str:
        """Compare a previous screenshot with the current page state.

        Parameters
        ----------
        screenshot_before:
            PNG bytes of the previous screenshot.
        question:
            Question about the differences.
        """
        before_b64 = base64.b64encode(screenshot_before).decode()
        after_b64 = await self._capture_b64()

        combined_prompt = f"""{question}

The first image is BEFORE and the second image is AFTER.
Describe any meaningful differences you observe."""

        # For models that support multiple images, send both.
        raw = await self._vision_request_multi(
            combined_prompt,
            [before_b64, after_b64],
            system=page_description_system(),
        )
        return raw

    # ── internals ─────────────────────────────────────────────────

    async def _capture_b64(self, *, full_page: bool = False) -> str:
        """Capture a PNG screenshot and return base64."""
        png_bytes = await self._page.screenshot(full_page=full_page)
        return base64.b64encode(png_bytes).decode()

    async def _vision_request(
        self, prompt: str, image_b64: str, *, system: str = ""
    ) -> str:
        """Send a vision request with one image.

        This builds the request format expected by OpenAI-compatible
        vision APIs (including Ollama with vision models).
        """
        if self._ai.config.backend == "ollama":
            return await self._ollama_vision(prompt, [image_b64], system)
        return await self._openai_vision(prompt, [image_b64], system)

    async def _vision_json_request(
        self, prompt: str, image_b64: str, *, system: str = ""
    ) -> dict[str, Any]:
        """Vision request that returns parsed JSON."""
        raw = await self._vision_request(prompt, image_b64, system=system)
        return self._ai._parse_json(raw)

    async def _vision_request_multi(
        self, prompt: str, images_b64: list[str], *, system: str = ""
    ) -> str:
        """Vision request with multiple images."""
        if self._ai.config.backend == "ollama":
            return await self._ollama_vision(prompt, images_b64, system)
        return await self._openai_vision(prompt, images_b64, system)

    # -- OpenAI-compatible vision --

    async def _openai_vision(
        self, prompt: str, images_b64: list[str], system: str
    ) -> str:
        import aiohttp

        session = await self._ai._get_session()
        url = f"{self._ai.config.base_url.rstrip('/')}/v1/chat/completions"

        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images_b64:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{img}",
                    "detail": "high",
                },
            })

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

        body = {
            "model": self._ai.config.model,
            "messages": messages,
            "max_tokens": self._ai.config.max_tokens,
            "temperature": self._ai.config.temperature,
        }

        async with session.post(url, json=body) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    # -- Ollama vision --

    async def _ollama_vision(
        self, prompt: str, images_b64: list[str], system: str
    ) -> str:
        import aiohttp

        session = await self._ai._get_session()
        url = f"{self._ai.config.base_url.rstrip('/')}/api/chat"

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({
            "role": "user",
            "content": prompt,
            "images": images_b64,
        })

        body = {
            "model": self._ai.config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self._ai.config.temperature,
                "num_predict": self._ai.config.max_tokens,
            },
        }

        async with session.post(url, json=body) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("message", {}).get("content", "")
