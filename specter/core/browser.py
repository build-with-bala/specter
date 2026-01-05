"""Browser lifecycle — find Chrome, launch it, connect CDP, tear down.

Specter always launches its own Chrome process so it controls the full
flag set (headless mode, debugging port, stealth flags, proxy, etc.).
A temporary user-data directory is created per session and cleaned up
on close unless ``persist_profile`` is set.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from specter.core.cdp_client import CDPClient, CDPSession

logger = logging.getLogger(__name__)


# ── configuration ──────────────────────────────────────────────────

@dataclass
class BrowserConfig:
    headless: bool = True
    debug_port: int = 9222
    user_data_dir: str | None = None
    persist_profile: bool = False
    proxy: str | None = None
    window_width: int = 1920
    window_height: int = 1080
    locale: str = "en-US"
    timezone: str | None = None
    stealth: bool = False        # handled by specter.stealth layer
    extra_args: list[str] = field(default_factory=list)

    def to_argv(self) -> list[str]:
        a = [
            f"--remote-debugging-port={self.debug_port}",
            f"--window-size={self.window_width},{self.window_height}",
            f"--lang={self.locale}",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--disable-translate",
            "--no-first-run",
            "--metrics-recording-only",
            "--safebrowsing-disable-auto-update",
            "--no-sandbox",
            "--disable-gpu",
        ]
        if self.headless:
            a.append("--headless=new")
        if self.proxy:
            a.append(f"--proxy-server={self.proxy}")
        if self.stealth:
            a.append("--disable-blink-features=AutomationControlled")
        if self.timezone:
            a.append(f"--timezone={self.timezone}")
        if self.user_data_dir:
            a.append(f"--user-data-dir={self.user_data_dir}")
        a.extend(self.extra_args)
        return a


# ── Chrome finder ──────────────────────────────────────────────────

_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium-browser",
    "/usr/bin/chromium",
]


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and os.path.isfile(env):
        return env
    for c in _CANDIDATES:
        if os.path.isfile(c):
            return c
    w = shutil.which("google-chrome") or shutil.which("chromium")
    if w:
        return w
    raise FileNotFoundError(
        "Chrome not found. Install Chrome or export CHROME_PATH=/path/to/chrome"
    )


# ── Browser class ──────────────────────────────────────────────────

class Browser:
    """Owns a Chrome process and exposes CDP sessions for each tab.

    Intended to be used as an async context manager::

        async with Browser(BrowserConfig(headless=False)) as browser:
            page = await browser.page()
            await page.goto("https://example.com")
    """

    def __init__(self, config: BrowserConfig | None = None):
        self.config = config or BrowserConfig()
        self._process: subprocess.Popen | None = None
        self._cdp: CDPClient | None = None
        self._tmp: str | None = None
        self._sessions: list[CDPSession] = []

    # ── public API ─────────────────────────────────────────────────

    async def launch(self) -> None:
        """Start Chrome and wait for the CDP endpoint."""
        chrome = find_chrome()

        if not self.config.user_data_dir:
            self._tmp = tempfile.mkdtemp(prefix="specter-")
            self.config.user_data_dir = self._tmp

        argv = [chrome, *self.config.to_argv()]
        logger.info("Launching %s", chrome)
        self._process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        await self._wait_cdp_ready()
        self._cdp = CDPClient(f"http://127.0.0.1:{self.config.debug_port}")

    async def connect(self, debug_url: str = "http://localhost:9222") -> None:
        """Attach to an already-running Chrome instance."""
        self._cdp = CDPClient(debug_url)

    async def new_page(self, url: str = "about:blank") -> CDPSession:
        """Open a new tab and return its CDP session."""
        assert self._cdp, "Browser not launched"
        sess = await self._cdp.new_tab(url)
        self._sessions.append(sess)
        return sess

    async def first_page(self) -> CDPSession:
        """Connect to the first existing tab."""
        assert self._cdp
        sess = await self._cdp.connect_tab()
        self._sessions.append(sess)
        return sess

    async def close(self) -> None:
        for s in self._sessions:
            try:
                await s.disconnect()
            except Exception:
                pass
        self._sessions.clear()

        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None

        if self._tmp and not self.config.persist_profile:
            shutil.rmtree(self._tmp, ignore_errors=True)

    # ── context manager ────────────────────────────────────────────

    async def __aenter__(self) -> "Browser":
        await self.launch()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    # ── internal ───────────────────────────────────────────────────

    async def _wait_cdp_ready(self, timeout: float = 15) -> None:
        url = f"http://127.0.0.1:{self.config.debug_port}/json/version"
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=2)) as r:
                        if r.status == 200:
                            return
            except Exception:
                pass
            await asyncio.sleep(0.25)
        raise TimeoutError("Chrome CDP not ready")
