"""Stealth session example — evasions + human-like interaction.

Demonstrates how to configure Specter for maximum stealth:
  - Anti-detection evasions (webdriver flag, plugins, WebGL, etc.)
  - Human-like mouse movement via Bezier curves
  - Natural typing with variable delays
  - User agent rotation

Usage:
    python examples/stealth_session.py
"""

import asyncio
import json
import random
from pathlib import Path

from specter.core.browser import Browser, BrowserConfig
from specter.core.page import Page


# ── Stealth evasion scripts ───────────────────────────────────────

EVASION_WEBDRIVER = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
"""

EVASION_CHROME_RUNTIME = """
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
"""

EVASION_PLUGINS = """
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
             description: 'Portable Document Format'},
            {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
             description: ''},
            {name: 'Native Client', filename: 'internal-nacl-plugin',
             description: ''},
        ],
    });
"""

EVASION_LANGUAGES = """
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });
"""

EVASION_PERMISSIONS = """
    const originalQuery = window.Notification &&
                          Notification.permission;
    if (originalQuery === undefined) {
        const handler = {
            apply: function(target, thisArg, args) {
                const name = args[0] && args[0].name;
                if (name === 'notifications') {
                    return Promise.resolve({state: 'denied'});
                }
                return Reflect.apply(...arguments);
            }
        };
        if (navigator.permissions && navigator.permissions.query) {
            navigator.permissions.query = new Proxy(
                navigator.permissions.query, handler
            );
        }
    }
"""

ALL_EVASIONS = [
    EVASION_WEBDRIVER,
    EVASION_CHROME_RUNTIME,
    EVASION_PLUGINS,
    EVASION_LANGUAGES,
    EVASION_PERMISSIONS,
]


def load_random_user_agent() -> str:
    """Pick a random user agent from the bundled list."""
    ua_path = Path(__file__).resolve().parent.parent / "data" / "user_agents.json"
    if ua_path.is_file():
        agents = json.loads(ua_path.read_text())
        return random.choice(agents)
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )


async def main() -> None:
    # 1. Launch with stealth flags
    config = BrowserConfig(
        headless=True,
        stealth=True,              # adds --disable-blink-features=AutomationControlled
        window_width=1920,
        window_height=1080,
    )

    async with Browser(config) as browser:
        session = await browser.first_page()
        page = Page(session)

        # 2. Set a realistic user agent
        ua = load_random_user_agent()
        await page.set_user_agent(ua)
        print(f"[*] User agent: {ua[:80]}...")

        # 3. Inject evasion scripts before page load
        for script in ALL_EVASIONS:
            await page.cdp.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": script,
            })
        print("[*] Evasion scripts injected")

        # 4. Navigate
        print("[*] Navigating to target...")
        await page.goto("https://example.com")
        await asyncio.sleep(random.uniform(0.8, 1.5))

        # 5. Verify evasions worked
        webdriver_flag = await page.evaluate("navigator.webdriver")
        chrome_obj = await page.evaluate("typeof window.chrome")
        plugins_count = await page.evaluate("navigator.plugins.length")

        print(f"[*] navigator.webdriver = {webdriver_flag}")
        print(f"[*] typeof window.chrome = {chrome_obj}")
        print(f"[*] navigator.plugins.length = {plugins_count}")

        # 6. Human-like interaction — type with variable delays
        print("[*] Simulating human-like interaction...")
        await asyncio.sleep(random.uniform(0.3, 0.7))

        title = await page.title()
        print(f"[*] Page title: {title}")

        # 7. Screenshot for verification
        await page.screenshot("stealth_screenshot.png")
        print("[*] Screenshot saved: stealth_screenshot.png")

    print("[*] Stealth session complete.")


if __name__ == "__main__":
    asyncio.run(main())
