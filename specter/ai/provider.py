"""AI model provider abstraction for Specter.

Supports two backends:
  * **Ollama** -- local models served by ``ollama serve`` (default).
  * **OpenAI-compatible** -- any API that speaks the ``/v1/chat/completions``
    protocol (OpenAI, Together, Groq, vLLM, LiteLLM, etc.).

Both backends expose the same two methods:

  * ``generate(prompt, ...)``      -- free-form text response.
  * ``generate_json(prompt, ...)`` -- response parsed as a JSON object.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# ── configuration ─────────────────────────────────────────────────

@dataclass
class ProviderConfig:
    """Immutable configuration for an AI provider."""

    backend: str = "ollama"                      # "ollama" | "openai"
    model: str = "llama3"                        # model name / id
    base_url: str = ""                           # auto-detected if empty
    api_key: str = ""                            # needed for openai-compat
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 120.0
    extra_params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.base_url:
            if self.backend == "ollama":
                self.base_url = os.getenv("OLLAMA_HOST", "http://localhost:11434")
            else:
                self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com")
        if not self.api_key and self.backend == "openai":
            self.api_key = os.getenv("OPENAI_API_KEY", "")


# ── provider ──────────────────────────────────────────────────────

class AIProvider:
    """Unified interface to local and remote LLMs.

    Usage::

        provider = AIProvider(ProviderConfig(backend="ollama", model="llama3"))
        text = await provider.generate("Summarise this page: ...")
        data = await provider.generate_json("Extract fields ...", schema_hint={...})
    """

    def __init__(self, config: ProviderConfig | None = None):
        self.config = config or ProviderConfig()
        self._session: aiohttp.ClientSession | None = None

    # ── lifecycle ─────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self.config.api_key:
                headers["Authorization"] = f"Bearer {self.config.api_key}"
            self._session = aiohttp.ClientSession(
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.config.timeout),
            )
        return self._session

    async def close(self) -> None:
        """Shut down the underlying HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── public API ────────────────────────────────────────────────

    async def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Return free-form text from the model.

        Parameters
        ----------
        prompt:
            User message content.
        system:
            Optional system prompt prepended to the conversation.
        temperature:
            Override the default sampling temperature.
        max_tokens:
            Override the default max output tokens.
        """
        messages = self._build_messages(system, prompt)
        return await self._chat(messages, temperature, max_tokens)

    async def generate_json(
        self,
        prompt: str,
        *,
        system: str = "",
        schema_hint: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Return a parsed JSON dict from the model.

        The prompt should instruct the model to respond *only* with JSON.
        If ``schema_hint`` is provided, it is appended to the prompt so
        the model knows the expected shape.

        Falls back to extracting the first JSON object from the response
        text if the model wraps it in markdown fences.
        """
        full_prompt = prompt
        if schema_hint:
            full_prompt += f"\n\nExpected JSON schema:\n```json\n{json.dumps(schema_hint, indent=2)}\n```"

        messages = self._build_messages(system, full_prompt)
        raw = await self._chat(messages, temperature, max_tokens, json_mode=True)
        return self._parse_json(raw)

    # ── internals ─────────────────────────────────────────────────

    def _build_messages(
        self, system: str, user: str
    ) -> list[dict[str, str]]:
        msgs: list[dict[str, str]] = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": user})
        return msgs

    async def _chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        *,
        json_mode: bool = False,
    ) -> str:
        if self.config.backend == "ollama":
            return await self._chat_ollama(messages, temperature, max_tokens, json_mode)
        return await self._chat_openai(messages, temperature, max_tokens, json_mode)

    # -- Ollama --------------------------------------------------

    async def _chat_ollama(
        self,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> str:
        session = await self._get_session()
        url = f"{self.config.base_url.rstrip('/')}/api/chat"
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else self.config.temperature,
                "num_predict": max_tokens or self.config.max_tokens,
            },
        }
        if json_mode:
            body["format"] = "json"
        body.update(self.config.extra_params)

        logger.debug("Ollama request → %s  model=%s", url, self.config.model)
        async with session.post(url, json=body) as resp:
            resp.raise_for_status()
            data = await resp.json()
            content = data.get("message", {}).get("content", "")
            logger.debug("Ollama response (%d chars)", len(content))
            return content

    # -- OpenAI-compatible ---------------------------------------

    async def _chat_openai(
        self,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> str:
        session = await self._get_session()
        url = f"{self.config.base_url.rstrip('/')}/v1/chat/completions"
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.config.extra_params)

        logger.debug("OpenAI request → %s  model=%s", url, self.config.model)
        async with session.post(url, json=body) as resp:
            resp.raise_for_status()
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            logger.debug("OpenAI response (%d chars)", len(content))
            return content

    # -- JSON extraction -----------------------------------------

    @staticmethod
    def _parse_json(raw: str) -> dict[str, Any]:
        """Best-effort extraction of a JSON object from model output."""
        text = raw.strip()

        # Attempt direct parse first.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Strip markdown code fences.
        fence_match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if fence_match:
            try:
                return json.loads(fence_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Find the first { ... } block.
        brace_match = re.search(r"\{.*\}", text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

        logger.warning("Failed to parse JSON from model output:\n%s", text[:500])
        return {}
