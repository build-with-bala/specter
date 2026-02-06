"""Data extraction example — navigate to a page, extract structured data.

Demonstrates Specter's ability to pull structured data from web pages
using CSS selectors and JavaScript evaluation.  For AI-powered
extraction, configure an AI provider in config/local.yaml.

Usage:
    python examples/data_extraction.py
"""

import asyncio
import json

from specter.core.browser import Browser, BrowserConfig
from specter.core.page import Page


async def extract_page_metadata(page: Page) -> dict:
    """Extract common metadata from any page."""
    return await page.evaluate("""(() => {
        const meta = (name) => {
            const el = document.querySelector(
                `meta[name="${name}"], meta[property="${name}"]`
            );
            return el ? el.content : null;
        };

        return {
            title: document.title,
            description: meta('description') || meta('og:description'),
            author: meta('author'),
            keywords: meta('keywords'),
            og_title: meta('og:title'),
            og_image: meta('og:image'),
            og_type: meta('og:type'),
            canonical: (() => {
                const link = document.querySelector('link[rel="canonical"]');
                return link ? link.href : null;
            })(),
            language: document.documentElement.lang || null,
        };
    })()""") or {}


async def extract_links(page: Page) -> list[dict]:
    """Extract all links from the page."""
    return await page.evaluate("""
        Array.from(document.querySelectorAll('a[href]')).map(a => ({
            text: a.textContent.trim().substring(0, 100),
            href: a.href,
            rel: a.rel || null,
            target: a.target || null,
        }))
    """) or []


async def extract_headings(page: Page) -> list[dict]:
    """Extract heading hierarchy."""
    return await page.evaluate("""
        Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6'))
            .map(h => ({
                level: parseInt(h.tagName.substring(1)),
                text: h.textContent.trim().substring(0, 200),
            }))
    """) or []


async def extract_images(page: Page) -> list[dict]:
    """Extract image information."""
    return await page.evaluate("""
        Array.from(document.querySelectorAll('img')).map(img => ({
            src: img.src,
            alt: img.alt || null,
            width: img.naturalWidth,
            height: img.naturalHeight,
        }))
    """) or []


async def extract_structured_data(page: Page) -> list[dict]:
    """Extract JSON-LD structured data from the page."""
    return await page.evaluate("""
        Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
            .map(script => {
                try { return JSON.parse(script.textContent); }
                catch { return null; }
            })
            .filter(Boolean)
    """) or []


async def main() -> None:
    config = BrowserConfig(
        headless=True,
        window_width=1920,
        window_height=1080,
    )

    async with Browser(config) as browser:
        session = await browser.first_page()
        page = Page(session)

        url = "https://example.com"
        print(f"[*] Navigating to {url}...")
        await page.goto(url)
        await asyncio.sleep(1.0)

        # Extract all data
        print("[*] Extracting page data...\n")

        metadata = await extract_page_metadata(page)
        links = await extract_links(page)
        headings = await extract_headings(page)
        images = await extract_images(page)
        structured = await extract_structured_data(page)

        result = {
            "url": url,
            "metadata": metadata,
            "headings": headings,
            "links": links,
            "images": images,
            "structured_data": structured,
            "stats": {
                "link_count": len(links),
                "heading_count": len(headings),
                "image_count": len(images),
                "structured_data_count": len(structured),
            },
        }

        # Print results
        output = json.dumps(result, indent=2, ensure_ascii=False)
        print(output)

        # Save to file
        output_path = "extracted_data.json"
        with open(output_path, "w") as f:
            f.write(output)
        print(f"\n[*] Results saved to {output_path}")

    print("[*] Done.")


if __name__ == "__main__":
    asyncio.run(main())
