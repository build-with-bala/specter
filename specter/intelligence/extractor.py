"""Structured data extraction from web pages.

Given a target schema (e.g. ``{"title": "string", "price": "number"}``),
this module extracts matching data from the page using two strategies:

  1. **DOM analysis** (fast, no LLM needed) -- uses CSS selectors and
     heuristics to find common patterns like tables, product cards,
     lists, and meta tags.
  2. **LLM extraction** (accurate, requires AI provider) -- sends the
     visible text to the model with the schema and asks for structured
     output.

Usage::

    from specter.intelligence.extractor import DataExtractor

    extractor = DataExtractor(page, ai_provider)
    items = await extractor.extract_many(
        {"title": "string", "price": "number", "rating": "number"},
    )
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from specter.core.page import Page
from specter.ai.provider import AIProvider
from specter.ai.prompts import data_extraction_prompt, data_extraction_system

logger = logging.getLogger(__name__)

# Maximum text length sent to the LLM.
_MAX_TEXT_SIZE = 15_000


# ── public API ────────────────────────────────────────────────────

class DataExtractor:
    """Extract structured data from a web page.

    Parameters
    ----------
    page:
        The ``Page`` to extract from.
    provider:
        An ``AIProvider`` for LLM-based extraction.  If ``None``,
        only heuristic strategies are available.
    """

    def __init__(self, page: Page, provider: AIProvider | None = None):
        self._page = page
        self._ai = provider

    # ── single-record extraction ──────────────────────────────────

    async def extract(
        self,
        schema: dict[str, str],
        *,
        use_llm: bool = True,
    ) -> dict[str, Any]:
        """Extract a single record matching *schema*.

        Parameters
        ----------
        schema:
            Mapping of ``field_name → type_hint``.  Supported hints:
            ``"string"``, ``"number"``, ``"boolean"``, ``"list"``.
        use_llm:
            If ``True`` (default) and an AI provider is configured,
            fall back to LLM extraction when heuristics come up short.

        Returns
        -------
        A dict whose keys are the schema field names and whose values
        are the extracted data (or ``None`` for missing fields).
        """
        # Start with DOM heuristics.
        result = await self._extract_dom(schema)

        # Fill gaps with LLM if available.
        missing = [k for k, v in result.items() if v is None]
        if missing and use_llm and self._ai:
            llm_data = await self._extract_llm(schema, multiple=False)
            for key in missing:
                if key in llm_data and llm_data[key] is not None:
                    result[key] = llm_data[key]

        return result

    # ── multi-record extraction ───────────────────────────────────

    async def extract_many(
        self,
        schema: dict[str, str],
        *,
        use_llm: bool = True,
        max_items: int = 50,
    ) -> list[dict[str, Any]]:
        """Extract a list of records matching *schema*.

        Designed for pages with repeated items (product listings,
        search results, tables, etc.).
        """
        # Try table extraction first.
        table_data = await self._extract_table(schema)
        if table_data:
            return table_data[:max_items]

        # Try repeated element extraction.
        list_data = await self._extract_repeated(schema)
        if list_data:
            return list_data[:max_items]

        # Fall back to LLM.
        if use_llm and self._ai:
            llm_data = await self._extract_llm(schema, multiple=True)
            if isinstance(llm_data, list):
                return llm_data[:max_items]

        return []

    # ── meta / JSON-LD extraction ─────────────────────────────────

    async def extract_metadata(self) -> dict[str, Any]:
        """Extract common metadata: title, description, og tags, JSON-LD."""
        meta: dict[str, Any] = {}

        meta["title"] = await self._page.title()

        js = """
        (() => {
            const m = {};
            // Meta tags.
            document.querySelectorAll('meta').forEach(el => {
                const name = el.getAttribute('name') || el.getAttribute('property') || '';
                const content = el.getAttribute('content') || '';
                if (name && content) m[name] = content;
            });
            // JSON-LD.
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            const jsonld = [];
            scripts.forEach(s => {
                try { jsonld.push(JSON.parse(s.textContent)); } catch(e) {}
            });
            if (jsonld.length) m['_jsonld'] = jsonld;
            // Canonical URL.
            const canon = document.querySelector('link[rel="canonical"]');
            if (canon) m['canonical'] = canon.getAttribute('href');
            return m;
        })()
        """
        raw = await self._page.evaluate(js)
        if raw and isinstance(raw, dict):
            meta.update(raw)

        return meta

    # ── DOM heuristic extraction ──────────────────────────────────

    async def _extract_dom(self, schema: dict[str, str]) -> dict[str, Any]:
        """Best-effort extraction using DOM queries."""
        result: dict[str, Any] = {k: None for k in schema}

        # Extract metadata first.
        meta = await self.extract_metadata()

        # Map common field names to metadata keys.
        meta_map: dict[str, list[str]] = {
            "title": ["title", "og:title", "twitter:title"],
            "description": ["description", "og:description", "twitter:description"],
            "image": ["og:image", "twitter:image"],
            "url": ["canonical", "og:url"],
            "author": ["author"],
            "date": ["article:published_time", "datePublished"],
        }
        for field_name in schema:
            fl = field_name.lower()
            for meta_field, meta_keys in meta_map.items():
                if fl == meta_field or meta_field in fl:
                    for mk in meta_keys:
                        if mk in meta and meta[mk]:
                            result[field_name] = meta[mk]
                            break

        # Try JSON-LD.
        jsonld = meta.get("_jsonld", [])
        for ld_item in jsonld:
            if isinstance(ld_item, dict):
                for field_name in schema:
                    if result[field_name] is None:
                        val = ld_item.get(field_name) or ld_item.get(field_name.lower())
                        if val is not None:
                            result[field_name] = _coerce(val, schema[field_name])

        # Attempt direct CSS selector queries for common patterns.
        selector_hints: dict[str, list[str]] = {
            "price": [".price", "[data-price]", ".product-price",
                      '[itemprop="price"]', ".cost"],
            "title": ["h1", ".title", '[itemprop="name"]', ".product-title"],
            "rating": [".rating", '[itemprop="ratingValue"]', ".stars"],
            "name": ["h1", ".name", '[itemprop="name"]'],
            "description": [".description", '[itemprop="description"]',
                            ".product-description"],
        }
        for field_name in schema:
            if result[field_name] is not None:
                continue
            fl = field_name.lower()
            selectors = selector_hints.get(fl, [f".{fl}", f"[data-{fl}]", f'[itemprop="{fl}"]'])
            for sel in selectors:
                try:
                    el = await self._page.query(sel)
                    if el and el.text:
                        result[field_name] = _coerce(el.text.strip(), schema[field_name])
                        break
                    if el:
                        text = await self._page.evaluate(
                            f'document.querySelector("{sel}")?.textContent?.trim()'
                        )
                        if text:
                            result[field_name] = _coerce(text, schema[field_name])
                            break
                except Exception:
                    continue

        return result

    # ── table extraction ──────────────────────────────────────────

    async def _extract_table(self, schema: dict[str, str]) -> list[dict[str, Any]]:
        """Extract data from HTML tables."""
        tables_js = """
        (() => {
            const results = [];
            document.querySelectorAll('table').forEach(table => {
                const headers = [];
                table.querySelectorAll('thead th, thead td, tr:first-child th, tr:first-child td')
                    .forEach(th => headers.push(th.textContent.trim().toLowerCase()));
                if (headers.length === 0) return;

                const rows = [];
                const bodyRows = table.querySelectorAll('tbody tr');
                const allRows = bodyRows.length ? bodyRows : table.querySelectorAll('tr');
                allRows.forEach((tr, i) => {
                    // Skip header row if it was in tbody.
                    const cells = tr.querySelectorAll('td');
                    if (cells.length === 0) return;
                    const row = {};
                    cells.forEach((td, j) => {
                        if (j < headers.length) row[headers[j]] = td.textContent.trim();
                    });
                    if (Object.keys(row).length > 0) rows.push(row);
                });
                if (rows.length) results.push({ headers, rows });
            });
            return results;
        })()
        """
        tables = await self._page.evaluate(tables_js) or []
        schema_lower = {k.lower(): (k, t) for k, t in schema.items()}

        for table in tables:
            headers = table.get("headers", [])
            rows = table.get("rows", [])
            if not rows:
                continue

            # Check if table headers match schema fields.
            matched_cols: dict[str, str] = {}
            for hdr in headers:
                if hdr in schema_lower:
                    matched_cols[hdr] = schema_lower[hdr][0]
                else:
                    for sk, (orig, _) in schema_lower.items():
                        if sk in hdr or hdr in sk:
                            matched_cols[hdr] = orig
                            break

            if len(matched_cols) < 1:
                continue

            result: list[dict[str, Any]] = []
            for row in rows:
                item: dict[str, Any] = {v: None for v in schema}
                for col_header, field_name in matched_cols.items():
                    val = row.get(col_header)
                    if val is not None:
                        item[field_name] = _coerce(val, schema[field_name])
                result.append(item)
            if result:
                return result

        return []

    # ── repeated element extraction ───────────────────────────────

    async def _extract_repeated(self, schema: dict[str, str]) -> list[dict[str, Any]]:
        """Find repeated DOM structures (cards, list items, etc.)."""
        repeated_js = """
        (() => {
            // Find parent elements whose children share the same tag and class.
            const candidates = [];
            const containers = document.querySelectorAll('ul, ol, [class*="list"], [class*="grid"], [class*="results"], main, [role="list"]');
            containers.forEach(container => {
                const children = Array.from(container.children).filter(
                    c => c.offsetParent !== null  // visible
                );
                if (children.length < 2) return;

                // Group by tag+class.
                const groups = {};
                children.forEach(c => {
                    const key = c.tagName + '.' + (c.className || '').split(' ').sort().join('.');
                    if (!groups[key]) groups[key] = [];
                    groups[key].push(c);
                });

                for (const [key, els] of Object.entries(groups)) {
                    if (els.length >= 2) {
                        candidates.push(els.map(el => el.textContent.trim().substring(0, 500)));
                    }
                }
            });
            return candidates.length ? candidates[0] : null;
        })()
        """
        items_text = await self._page.evaluate(repeated_js)
        if not items_text or not self._ai:
            return []

        # Use LLM to parse the repeated text items.
        schema_json = json.dumps(schema, indent=2)
        prompt = f"""Extract structured data from each of these repeated items.

Schema:
```json
{schema_json}
```

Items:
{chr(10).join(f"--- Item {i+1} ---{chr(10)}{txt}" for i, txt in enumerate(items_text[:20]))}

Return a JSON array of objects matching the schema.
Respond with raw JSON only."""

        data = await self._ai.generate_json(prompt, system=data_extraction_system())
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        # If the LLM returned the array as a dict wrapper, try to unpack.
        for v in data.values():
            if isinstance(v, list):
                return v
        return []

    # ── LLM extraction ────────────────────────────────────────────

    async def _extract_llm(
        self,
        schema: dict[str, str],
        *,
        multiple: bool = False,
    ) -> Any:
        """Send page text to the LLM for structured extraction."""
        assert self._ai, "No AI provider configured"

        text = await self._page.text_content()
        if not text:
            text = await self._page.evaluate("document.body?.innerText || ''") or ""

        if len(text) > _MAX_TEXT_SIZE:
            text = text[:_MAX_TEXT_SIZE]

        prompt = data_extraction_prompt(
            schema, text,
            page_url=self._page.url,
            multiple=multiple,
        )
        system = data_extraction_system()
        data = await self._ai.generate_json(prompt, system=system)

        if multiple:
            # Try to find the array in the response.
            if isinstance(data, list):
                return data
            for v in data.values():
                if isinstance(v, list):
                    return v
            return []

        return data


# ── helpers ───────────────────────────────────────────────────────

def _coerce(value: Any, type_hint: str) -> Any:
    """Coerce a raw extracted value to the target type."""
    if value is None:
        return None

    th = type_hint.lower()
    if th == "number":
        text = str(value)
        # Strip currency symbols and commas.
        cleaned = re.sub(r"[^\d.\-]", "", text)
        if cleaned:
            try:
                return float(cleaned) if "." in cleaned else int(cleaned)
            except ValueError:
                pass
        return None

    if th == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).lower().strip()
        return text in ("true", "yes", "1", "on")

    if th == "list":
        if isinstance(value, list):
            return value
        return [str(value)]

    # Default: string.
    return str(value).strip()
