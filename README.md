# Specter

```
   ____                  _
  / ___| _ __   ___  ___| |_ ___ _ __
  \___ \| '_ \ / _ \/ __| __/ _ \ '__|
   ___) | |_) |  __/ (__| ||  __/ |
  |____/| .__/ \___|\___|\__\___|_|
        |_|
  Autonomous Browser Agent  v2.0.0
```

**Specter** is an autonomous browser automation agent built on raw Chrome DevTools Protocol (CDP) control. No wrapper libraries, no middleware -- direct WebSocket communication with Chrome for maximum speed and undetectable operation.

## Why Specter?

| Feature | Selenium | Playwright | Puppeteer | **Specter** |
|---|---|---|---|---|
| Protocol | WebDriver (HTTP) | CDP + custom | CDP via JS | **Raw CDP (WebSocket)** |
| Detection resistance | None | Low | Low | **Built-in evasion suite** |
| Human-like input | No | No | No | **Bezier curves, variable delays** |
| AI integration | No | No | No | **Multi-provider (GPT-4o, Claude, Ollama)** |
| Vision understanding | No | No | No | **Screenshot-to-action via LLM** |
| Record & replay | No | Codegen | No | **JSON-based session recording** |
| Async from ground up | Partial | Yes | Yes | **Yes (asyncio native)** |
| Dependencies | Heavy | Heavy | Moderate | **Minimal (websockets + aiohttp)** |

## Installation

```bash
# Clone the repository
git clone https://github.com/specter-agent/specter.git
cd specter

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -e .

# Optional: AI provider support
pip install -e ".[ai]"

# Optional: development tools
pip install -e ".[dev]"
```

### Requirements

- Python 3.11+
- Google Chrome or Chromium installed

## Quick Start

### Basic Navigation

```python
import asyncio
from specter.core.browser import Browser, BrowserConfig
from specter.core.page import Page

async def main():
    async with Browser(BrowserConfig(headless=True)) as browser:
        session = await browser.first_page()
        page = Page(session)

        await page.goto("https://example.com")
        title = await page.title()
        print(f"Title: {title}")

        await page.screenshot("page.png")

asyncio.run(main())
```

### Stealth Mode

```python
import asyncio
from specter.core.browser import Browser, BrowserConfig
from specter.core.page import Page

async def main():
    config = BrowserConfig(headless=True, stealth=True)

    async with Browser(config) as browser:
        session = await browser.first_page()
        page = Page(session)

        # Inject evasions before navigating
        await page.cdp.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        })

        await page.set_user_agent(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )

        await page.goto("https://example.com")

asyncio.run(main())
```

### Data Extraction

```python
import asyncio
from specter.core.browser import Browser, BrowserConfig
from specter.core.page import Page

async def main():
    async with Browser() as browser:
        session = await browser.first_page()
        page = Page(session)

        await page.goto("https://example.com")

        # Extract structured data via JS
        data = await page.evaluate("""(() => {
            return {
                title: document.title,
                headings: Array.from(document.querySelectorAll('h1, h2, h3'))
                    .map(h => h.textContent.trim()),
                links: Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({text: a.textContent.trim(), href: a.href})),
            };
        })()""")

        print(data)

asyncio.run(main())
```

## Architecture

```
specter/
|
|-- core/                  # Foundation layer (zero external AI deps)
|   |-- cdp_client.py      #   WebSocket JSON-RPC transport
|   |-- browser.py          #   Chrome lifecycle (find, launch, connect)
|   |-- page.py             #   Tab controller (nav, DOM, input, screenshots)
|   |-- element.py          #   Chainable element wrapper
|   |-- types.py            #   Shared dataclasses (Box, Point, Cookie, ...)
|
|-- stealth/               # Anti-detection & human simulation
|   |-- evasions.py         #   JS injection scripts (webdriver, plugins, ...)
|   |-- human.py            #   Bezier mouse paths, typing delays
|   |-- fingerprint.py      #   Canvas, WebGL, audio fingerprint noise
|
|-- network/               # Network layer
|   |-- intercept.py        #   Request interception & modification
|   |-- recorder.py         #   HAR-like traffic capture
|   |-- proxy.py            #   Proxy chain management
|
|-- ai/                    # AI-powered intelligence
|   |-- providers.py        #   Multi-provider abstraction (OpenAI, Claude, Ollama)
|   |-- vision.py           #   Screenshot analysis & element location
|   |-- extractor.py        #   Structured data extraction via LLM
|   |-- planner.py          #   Multi-step task decomposition
|
|-- intelligence/          # High-level agent capabilities
|   |-- selector.py         #   Self-healing CSS/XPath selectors
|   |-- explorer.py         #   Autonomous page exploration
|   |-- forms.py            #   Intelligent form filling
|
|-- automation/            # Orchestration
|   |-- recorder.py         #   Action recording (click, type, navigate)
|   |-- replayer.py         #   Session replay from JSON
|   |-- scheduler.py        #   Cron-like task scheduling
|
|-- config.py              # YAML configuration loader
|-- cli.py                 # Click-based CLI
|
config/
|-- default.yaml           # Default settings
|-- local.yaml             # User overrides (gitignored)
|
data/
|-- user_agents.json       # Realistic Chrome UA strings
|
tests/                     # Unit & integration tests
examples/                  # Ready-to-run scripts
```

## CLI Usage

Specter ships with a CLI for common tasks:

```bash
# Run an automation script
specter run examples/basic_navigation.py

# Run with stealth mode
specter run script.py --stealth --no-headless

# Take a screenshot
specter screenshot https://example.com --output page.png --full-page

# Record a browser session (opens visible browser)
specter record https://example.com --output session.json

# Replay a recorded session
specter replay session.json --speed 2.0

# Extract structured data
specter extract https://example.com --schema schema.json --output data.json

# List configured AI providers
specter providers
```

## AI Provider Setup

Specter supports multiple AI backends for vision, extraction, and autonomous planning.

### OpenAI

```bash
export OPENAI_API_KEY=sk-...
```

Or in `config/local.yaml`:

```yaml
ai:
  default_provider: openai
  providers:
    openai:
      api_key: "sk-..."
      model: "gpt-4o"
```

### Anthropic

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

### Ollama (local, no API key needed)

```bash
# Install Ollama: https://ollama.ai
ollama pull llama3
```

```yaml
ai:
  default_provider: ollama
  providers:
    ollama:
      model: "llama3"
      base_url: "http://localhost:11434"
```

## Configuration

Specter loads configuration in this order (later overrides earlier):

1. `config/default.yaml` -- shipped defaults
2. `config/local.yaml` -- your overrides (gitignored)
3. CLI flags (`--headless`, `--stealth`, etc.)
4. Environment variables (`SPECTER_HEADLESS`, `SPECTER_PROXY`, etc.)

### Key Settings

```yaml
browser:
  headless: true
  proxy: "http://127.0.0.1:8080"
  window_width: 1920
  window_height: 1080

stealth:
  enabled: true
  evasions:
    webdriver: true
    canvas_fingerprint: true
  human_input:
    typing_wpm: 80
    mouse_bezier_steps: 20

network:
  block_resource_types: ["Image", "Font", "Media"]
  request_timeout: 30.0
```

See `config/default.yaml` for the full reference.

## Running Tests

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## Disclaimer

Specter is provided for **authorized security testing, research, and educational purposes only**. Users are solely responsible for ensuring their use complies with all applicable laws and regulations. Do not use this tool to access systems without explicit authorization. The authors assume no liability for misuse.

## License

[MIT](LICENSE)
