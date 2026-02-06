"""Basic navigation example — launch browser, visit a page, screenshot.

Usage:
    python examples/basic_navigation.py
"""

import asyncio

from specter.core.browser import Browser, BrowserConfig
from specter.core.page import Page


async def main() -> None:
    # 1. Configure the browser
    config = BrowserConfig(
        headless=True,
        window_width=1920,
        window_height=1080,
    )

    # 2. Launch and connect
    async with Browser(config) as browser:
        session = await browser.first_page()
        page = Page(session)

        # 3. Navigate to a page
        print("[*] Navigating to example.com...")
        await page.goto("https://example.com")

        # 4. Get the page title
        title = await page.title()
        print(f"[*] Page title: {title}")

        # 5. Get the current URL
        print(f"[*] URL: {page.url}")

        # 6. Take a screenshot
        screenshot_path = "example_screenshot.png"
        data = await page.screenshot(screenshot_path)
        print(f"[*] Screenshot saved: {screenshot_path} ({len(data):,} bytes)")

        # 7. Extract some text
        heading = await page.evaluate(
            "document.querySelector('h1')?.textContent"
        )
        print(f"[*] Page heading: {heading}")

        # 8. Get all links on the page
        links = await page.evaluate("""
            Array.from(document.querySelectorAll('a'))
                 .map(a => ({text: a.textContent.trim(), href: a.href}))
        """)
        print(f"[*] Links found: {links}")

    print("[*] Browser closed. Done.")


if __name__ == "__main__":
    asyncio.run(main())
