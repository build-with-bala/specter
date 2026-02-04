"""Specter CLI — command-line interface for browser automation.

Usage::

    specter run script.py
    specter record https://example.com
    specter replay recording.json
    specter screenshot https://example.com --output page.png
    specter extract https://example.com --schema schema.json
    specter providers
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import click

from specter import __version__

BANNER = r"""
   ____                  _
  / ___| _ __   ___  ___| |_ ___ _ __
  \___ \| '_ \ / _ \/ __| __/ _ \ '__|
   ___) | |_) |  __/ (__| ||  __/ |
  |____/| .__/ \___|\___|\__\___|_|
        |_|
  Autonomous Browser Agent  v{version}
  ─────────────────────────────────────
""".format(version=__version__)


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )


def _run_async(coro):
    """Run an async coroutine in a new event loop."""
    return asyncio.run(coro)


# ── CLI group ─────────────────────────────────────────────────────


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
@click.version_option(__version__, prog_name="specter")
def cli(verbose: bool) -> None:
    """Specter -- autonomous browser agent with direct CDP control."""
    _setup_logging("DEBUG" if verbose else "INFO")


# ── specter run ───────────────────────────────────────────────────


@cli.command()
@click.argument("script", type=click.Path(exists=True, dir_okay=False))
@click.option("--headless/--no-headless", default=True, help="Run headless.")
@click.option("--stealth", is_flag=True, help="Enable stealth mode.")
@click.option("--config", "config_path", type=click.Path(exists=True),
              default=None, help="Path to a YAML config file.")
def run(script: str, headless: bool, stealth: bool,
        config_path: str | None) -> None:
    """Run a Specter automation script."""
    click.echo(BANNER)
    click.echo(f"  Running: {script}")
    click.echo()

    spec = importlib.util.spec_from_file_location("user_script", script)
    if spec is None or spec.loader is None:
        click.secho(f"Error: cannot load {script}", fg="red", err=True)
        sys.exit(1)

    module = importlib.util.module_from_spec(spec)

    # Inject runtime config into the module namespace
    module.__dict__["__specter_headless__"] = headless
    module.__dict__["__specter_stealth__"] = stealth
    module.__dict__["__specter_config__"] = config_path

    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]

        # If the script defines a main() coroutine, run it
        if hasattr(module, "main"):
            main_fn = module.main
            if asyncio.iscoroutinefunction(main_fn):
                _run_async(main_fn())
            else:
                main_fn()

        click.secho("  Done.", fg="green")
    except KeyboardInterrupt:
        click.secho("\n  Interrupted.", fg="yellow")
        sys.exit(130)
    except Exception as exc:
        click.secho(f"  Error: {exc}", fg="red", err=True)
        sys.exit(1)


# ── specter record ────────────────────────────────────────────────


@cli.command()
@click.argument("url")
@click.option("--output", "-o", default=None,
              help="Output file (default: recording_<timestamp>.json).")
@click.option("--headless/--no-headless", default=False,
              help="Run headless (default: visible).")
def record(url: str, output: str | None, headless: bool) -> None:
    """Open a browser, record user actions, and save to JSON."""
    click.echo(BANNER)
    click.echo(f"  Recording session at: {url}")
    click.echo("  Press Ctrl+C to stop recording.\n")

    output_path = output or f"recording_{int(time.time())}.json"

    async def _record() -> None:
        from specter.core.browser import Browser, BrowserConfig

        config = BrowserConfig(headless=headless)
        async with Browser(config) as browser:
            session = await browser.first_page()

            from specter.core.page import Page
            page = Page(session)
            await page.goto(url)

            actions: list[dict[str, Any]] = []
            start = time.time()

            click.echo("  Browser launched. Interact with the page.")
            click.echo(f"  Actions will be saved to: {output_path}\n")

            # Record navigation events
            def on_frame_navigated(params: dict) -> None:
                frame = params.get("frame", {})
                if not frame.get("parentId"):
                    actions.append({
                        "kind": "navigate",
                        "url": frame.get("url", ""),
                        "timestamp": time.time() - start,
                    })

            session.on("Page.frameNavigated", on_frame_navigated)

            try:
                # Keep the session alive until interrupted
                while True:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                pass

            return actions

    try:
        actions = _run_async(_record())
    except KeyboardInterrupt:
        actions = []

    recording = {
        "version": __version__,
        "url": url,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actions": actions if actions else [],
    }

    Path(output_path).write_text(json.dumps(recording, indent=2))
    click.secho(f"\n  Saved recording to: {output_path}", fg="green")


# ── specter replay ────────────────────────────────────────────────


@cli.command()
@click.argument("recording", type=click.Path(exists=True, dir_okay=False))
@click.option("--headless/--no-headless", default=True,
              help="Run headless.")
@click.option("--speed", default=1.0,
              help="Playback speed multiplier (default: 1.0).")
def replay(recording: str, headless: bool, speed: float) -> None:
    """Replay a previously recorded session."""
    click.echo(BANNER)
    click.echo(f"  Replaying: {recording}")
    click.echo(f"  Speed: {speed}x\n")

    data = json.loads(Path(recording).read_text())
    actions = data.get("actions", [])
    start_url = data.get("url", "about:blank")

    if not actions:
        click.secho("  No actions found in recording.", fg="yellow")
        return

    async def _replay() -> None:
        from specter.core.browser import Browser, BrowserConfig
        from specter.core.page import Page

        config = BrowserConfig(headless=headless)
        async with Browser(config) as browser:
            session = await browser.first_page()
            page = Page(session)

            click.echo(f"  Navigating to: {start_url}")
            await page.goto(start_url)

            prev_ts = 0.0
            for i, action in enumerate(actions, 1):
                kind = action.get("kind", "")
                ts = action.get("timestamp", 0.0)

                # Wait for the appropriate delay
                delay = (ts - prev_ts) / speed
                if delay > 0:
                    await asyncio.sleep(delay)
                prev_ts = ts

                if kind == "navigate":
                    url = action.get("url", "")
                    click.echo(f"  [{i}/{len(actions)}] navigate -> {url}")
                    await page.goto(url)
                elif kind == "click":
                    sel = action.get("selector", "")
                    click.echo(f"  [{i}/{len(actions)}] click -> {sel}")
                    try:
                        await page.click(sel)
                    except Exception as e:
                        click.secho(f"    Warning: {e}", fg="yellow")
                elif kind == "type":
                    sel = action.get("selector", "")
                    text = action.get("text", "")
                    click.echo(f"  [{i}/{len(actions)}] type -> {sel}")
                    try:
                        await page.type_text(sel, text)
                    except Exception as e:
                        click.secho(f"    Warning: {e}", fg="yellow")
                elif kind == "scroll":
                    dy = action.get("scroll_y", 0)
                    click.echo(f"  [{i}/{len(actions)}] scroll -> dy={dy}")
                    await page.scroll(dy=dy)
                elif kind == "wait":
                    wait_time = action.get("meta", {}).get("duration", 1.0)
                    click.echo(f"  [{i}/{len(actions)}] wait -> {wait_time}s")
                    await asyncio.sleep(wait_time / speed)
                else:
                    click.echo(f"  [{i}/{len(actions)}] {kind} (skipped)")

        click.secho("\n  Replay complete.", fg="green")

    _run_async(_replay())


# ── specter screenshot ────────────────────────────────────────────


@cli.command()
@click.argument("url")
@click.option("--output", "-o", default="screenshot.png",
              help="Output file path (default: screenshot.png).")
@click.option("--full-page", is_flag=True, help="Capture the full page.")
@click.option("--headless/--no-headless", default=True, help="Run headless.")
@click.option("--width", default=1920, help="Viewport width.")
@click.option("--height", default=1080, help="Viewport height.")
def screenshot(url: str, output: str, full_page: bool,
               headless: bool, width: int, height: int) -> None:
    """Take a screenshot of a URL."""
    click.echo(BANNER)
    click.echo(f"  URL:    {url}")
    click.echo(f"  Output: {output}\n")

    async def _screenshot() -> None:
        from specter.core.browser import Browser, BrowserConfig
        from specter.core.page import Page

        config = BrowserConfig(
            headless=headless,
            window_width=width,
            window_height=height,
        )
        async with Browser(config) as browser:
            session = await browser.first_page()
            page = Page(session)

            await page.set_viewport(width, height)
            await page.goto(url)
            await asyncio.sleep(1.0)  # let rendering settle

            data = await page.screenshot(output, full_page=full_page)
            click.secho(f"  Saved {len(data):,} bytes to {output}", fg="green")

    _run_async(_screenshot())


# ── specter extract ───────────────────────────────────────────────


@cli.command()
@click.argument("url")
@click.option("--schema", "-s", "schema_path", required=True,
              type=click.Path(exists=True, dir_okay=False),
              help="JSON schema defining the data to extract.")
@click.option("--output", "-o", default=None,
              help="Output JSON file (default: stdout).")
@click.option("--headless/--no-headless", default=True, help="Run headless.")
@click.option("--provider", default=None,
              help="AI provider to use (default: from config).")
def extract(url: str, schema_path: str, output: str | None,
            headless: bool, provider: str | None) -> None:
    """Extract structured data from a URL using an AI provider."""
    click.echo(BANNER)
    click.echo(f"  URL:    {url}")
    click.echo(f"  Schema: {schema_path}\n")

    schema = json.loads(Path(schema_path).read_text())

    async def _extract() -> dict:
        from specter.core.browser import Browser, BrowserConfig
        from specter.core.page import Page

        config = BrowserConfig(headless=headless)
        async with Browser(config) as browser:
            session = await browser.first_page()
            page = Page(session)

            await page.goto(url)
            await asyncio.sleep(2.0)

            # Get page text content for extraction
            content = await page.text_content()
            page_title = await page.title()

            # Build extraction prompt
            prompt = (
                f"Extract structured data from this web page content "
                f"according to the following JSON schema.\n\n"
                f"Page title: {page_title}\n"
                f"Page URL: {url}\n\n"
                f"Schema:\n{json.dumps(schema, indent=2)}\n\n"
                f"Page content:\n{content[:8000]}\n\n"
                f"Return ONLY valid JSON matching the schema."
            )

            # For now, return the page metadata as structured data
            # Full AI extraction requires a configured provider
            result = {
                "_meta": {
                    "url": url,
                    "title": page_title,
                    "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                 time.gmtime()),
                    "schema": schema_path,
                    "content_length": len(content),
                    "provider": provider or "pending_configuration",
                },
                "_prompt": prompt,
            }
            return result

    result = _run_async(_extract())

    formatted = json.dumps(result, indent=2, ensure_ascii=False)
    if output:
        Path(output).write_text(formatted)
        click.secho(f"  Saved to {output}", fg="green")
    else:
        click.echo(formatted)


# ── specter providers ─────────────────────────────────────────────


@cli.command()
def providers() -> None:
    """List available AI providers and their status."""
    click.echo(BANNER)
    click.echo("  Available AI Providers")
    click.echo("  ─────────────────────\n")

    import os

    provider_info = [
        {
            "name": "OpenAI",
            "env_var": "OPENAI_API_KEY",
            "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"],
            "features": ["vision", "extraction", "selectors"],
        },
        {
            "name": "Anthropic",
            "env_var": "ANTHROPIC_API_KEY",
            "models": ["claude-sonnet-4-20250514", "claude-haiku-4-20250414"],
            "features": ["vision", "extraction", "selectors"],
        },
        {
            "name": "Ollama (local)",
            "env_var": None,
            "models": ["llama3", "mistral", "codellama"],
            "features": ["extraction", "selectors"],
        },
    ]

    for p in provider_info:
        name = p["name"]
        env = p["env_var"]

        if env is None:
            status = "local"
            color = "blue"
        elif os.environ.get(env):
            status = "configured"
            color = "green"
        else:
            status = "not configured"
            color = "yellow"

        click.echo(f"  {name}")
        click.secho(f"    Status:   {status}", fg=color)
        if env:
            click.echo(f"    Env var:  {env}")
        click.echo(f"    Models:   {', '.join(p['models'])}")
        click.echo(f"    Features: {', '.join(p['features'])}")
        click.echo()


# ── Entry point ───────────────────────────────────────────────────


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
