"""Browser fingerprint spoofing.

Injects scripts that modify the browser's observable fingerprint to
defeat canvas, WebGL, AudioContext, and screen-based tracking.

Techniques:
  * **Canvas noise** -- add imperceptible pixel-level noise to
    ``CanvasRenderingContext2D.getImageData`` and ``toDataURL`` /
    ``toBlob``, so every call returns a unique hash.
  * **WebGL vendor/renderer** -- override ``UNMASKED_VENDOR_WEBGL``
    and ``UNMASKED_RENDERER_WEBGL`` via ``getParameter``.
  * **Screen resolution** -- spoof ``screen.width``, ``screen.height``,
    ``screen.availWidth``, ``screen.availHeight``, ``devicePixelRatio``.
  * **Timezone** -- override ``Intl.DateTimeFormat`` resolved options
    and ``Date.prototype.getTimezoneOffset``.

Usage::

    from specter.stealth.fingerprint import FingerprintConfig, apply_fingerprint
    config = FingerprintConfig(
        webgl_vendor="Intel Inc.",
        webgl_renderer="Intel Iris OpenGL Engine",
        screen_width=1920,
        screen_height=1080,
        timezone="America/New_York",
    )
    await apply_fingerprint(cdp_session, config)
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from typing import Any

from specter.core.cdp_client import CDPSession

logger = logging.getLogger(__name__)


# ── configuration ─────────────────────────────────────────────────

# Pre-built realistic GPU profiles for spoofing.
GPU_PROFILES: list[dict[str, str]] = [
    {"vendor": "Intel Inc.", "renderer": "Intel Iris OpenGL Engine"},
    {"vendor": "Intel Inc.", "renderer": "Intel(R) UHD Graphics 630"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1080 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, AMD Radeon Pro 5500M OpenGL Engine, OpenGL 4.1)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"},
    {"vendor": "Apple", "renderer": "Apple M1"},
    {"vendor": "Apple", "renderer": "Apple M2 Pro"},
]

SCREEN_PROFILES: list[dict[str, int]] = [
    {"width": 1920, "height": 1080, "avail_width": 1920, "avail_height": 1040, "color_depth": 24, "pixel_ratio": 1},
    {"width": 2560, "height": 1440, "avail_width": 2560, "avail_height": 1400, "color_depth": 24, "pixel_ratio": 1},
    {"width": 1440, "height": 900, "avail_width": 1440, "avail_height": 860, "color_depth": 24, "pixel_ratio": 2},
    {"width": 1680, "height": 1050, "avail_width": 1680, "avail_height": 1010, "color_depth": 24, "pixel_ratio": 1},
    {"width": 1536, "height": 864, "avail_width": 1536, "avail_height": 824, "color_depth": 24, "pixel_ratio": 1},
    {"width": 2560, "height": 1600, "avail_width": 2560, "avail_height": 1575, "color_depth": 30, "pixel_ratio": 2},
]


@dataclass
class FingerprintConfig:
    """Fingerprint spoofing parameters.

    Leave fields as ``None`` to auto-pick a random realistic value.
    """

    # Canvas
    canvas_noise: bool = True
    noise_intensity: float = 0.02          # 0.0 = no noise, 1.0 = max noise

    # WebGL
    webgl_vendor: str | None = None
    webgl_renderer: str | None = None

    # Screen
    screen_width: int | None = None
    screen_height: int | None = None
    avail_width: int | None = None
    avail_height: int | None = None
    color_depth: int = 24
    pixel_ratio: float | None = None

    # Timezone
    timezone: str | None = None            # e.g. "America/New_York"
    timezone_offset: int | None = None     # minutes, e.g. 300 for EST

    # Hardware
    hardware_concurrency: int | None = None
    device_memory: float | None = None     # GB

    def resolve(self) -> "FingerprintConfig":
        """Fill in ``None`` fields with random plausible values."""
        cfg = FingerprintConfig(
            canvas_noise=self.canvas_noise,
            noise_intensity=self.noise_intensity,
            timezone=self.timezone,
            timezone_offset=self.timezone_offset,
        )

        # GPU
        if self.webgl_vendor and self.webgl_renderer:
            cfg.webgl_vendor = self.webgl_vendor
            cfg.webgl_renderer = self.webgl_renderer
        else:
            gpu = random.choice(GPU_PROFILES)
            cfg.webgl_vendor = self.webgl_vendor or gpu["vendor"]
            cfg.webgl_renderer = self.webgl_renderer or gpu["renderer"]

        # Screen
        if self.screen_width and self.screen_height:
            cfg.screen_width = self.screen_width
            cfg.screen_height = self.screen_height
            cfg.avail_width = self.avail_width or self.screen_width
            cfg.avail_height = self.avail_height or self.screen_height - 40
            cfg.pixel_ratio = self.pixel_ratio or 1.0
            cfg.color_depth = self.color_depth
        else:
            scr = random.choice(SCREEN_PROFILES)
            cfg.screen_width = self.screen_width or scr["width"]
            cfg.screen_height = self.screen_height or scr["height"]
            cfg.avail_width = self.avail_width or scr["avail_width"]
            cfg.avail_height = self.avail_height or scr["avail_height"]
            cfg.color_depth = self.color_depth or scr["color_depth"]
            cfg.pixel_ratio = self.pixel_ratio or scr["pixel_ratio"]

        # Hardware
        cfg.hardware_concurrency = self.hardware_concurrency or random.choice([4, 8, 12, 16])
        cfg.device_memory = self.device_memory or random.choice([4.0, 8.0, 16.0])

        return cfg


# ── script generators ─────────────────────────────────────────────

def _canvas_noise_script(intensity: float) -> str:
    """Generate JS that adds imperceptible noise to canvas reads."""
    return f"""
(function() {{
    const NOISE = {intensity};
    const _seed = Math.floor(Math.random() * 1000000);

    // Simple seeded PRNG for deterministic-per-session noise.
    function _noise(seed) {{
        let x = Math.sin(seed) * 10000;
        return x - Math.floor(x);
    }}

    // Patch getImageData to add noise.
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function() {{
        const imageData = _origGetImageData.apply(this, arguments);
        const data = imageData.data;
        for (let i = 0; i < data.length; i += 4) {{
            // Only modify RGB, leave alpha alone.
            const n = _noise(_seed + i) * NOISE * 2 - NOISE;
            data[i]     = Math.max(0, Math.min(255, data[i]     + Math.floor(n * 255)));
            data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + Math.floor(n * 255)));
            data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + Math.floor(n * 255)));
        }}
        return imageData;
    }};
    CanvasRenderingContext2D.prototype.getImageData.toString = () => 'function getImageData() {{ [native code] }}';

    // Patch toDataURL.
    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {{
        const ctx = this.getContext('2d');
        if (ctx) {{
            // Trigger noise injection by reading and re-writing pixel data.
            try {{
                const img = ctx.getImageData(0, 0, this.width, this.height);
                ctx.putImageData(img, 0, 0);
            }} catch(e) {{}}
        }}
        return _origToDataURL.apply(this, arguments);
    }};
    HTMLCanvasElement.prototype.toDataURL.toString = () => 'function toDataURL() {{ [native code] }}';

    // Patch toBlob.
    const _origToBlob = HTMLCanvasElement.prototype.toBlob;
    HTMLCanvasElement.prototype.toBlob = function(callback, type, quality) {{
        const ctx = this.getContext('2d');
        if (ctx) {{
            try {{
                const img = ctx.getImageData(0, 0, this.width, this.height);
                ctx.putImageData(img, 0, 0);
            }} catch(e) {{}}
        }}
        return _origToBlob.apply(this, arguments);
    }};
    HTMLCanvasElement.prototype.toBlob.toString = () => 'function toBlob() {{ [native code] }}';
}})();
"""


def _webgl_script(vendor: str, renderer: str) -> str:
    """Generate JS to spoof WebGL vendor/renderer strings."""
    return f"""
(function() {{
    const VENDOR = "{vendor}";
    const RENDERER = "{renderer}";

    const _origGetParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {{
        const dbgInfo = this.getExtension('WEBGL_debug_renderer_info');
        if (dbgInfo) {{
            if (param === dbgInfo.UNMASKED_VENDOR_WEBGL) return VENDOR;
            if (param === dbgInfo.UNMASKED_RENDERER_WEBGL) return RENDERER;
        }}
        return _origGetParameter.call(this, param);
    }};
    WebGLRenderingContext.prototype.getParameter.toString = () => 'function getParameter() {{ [native code] }}';

    // Same for WebGL2.
    if (typeof WebGL2RenderingContext !== 'undefined') {{
        const _origGetParam2 = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {{
            const dbgInfo = this.getExtension('WEBGL_debug_renderer_info');
            if (dbgInfo) {{
                if (param === dbgInfo.UNMASKED_VENDOR_WEBGL) return VENDOR;
                if (param === dbgInfo.UNMASKED_RENDERER_WEBGL) return RENDERER;
            }}
            return _origGetParam2.call(this, param);
        }};
        WebGL2RenderingContext.prototype.getParameter.toString = () => 'function getParameter() {{ [native code] }}';
    }}
}})();
"""


def _screen_script(
    width: int, height: int,
    avail_width: int, avail_height: int,
    color_depth: int, pixel_ratio: float,
) -> str:
    """Generate JS to spoof screen properties."""
    return f"""
(function() {{
    Object.defineProperty(screen, 'width',       {{ get: () => {width} }});
    Object.defineProperty(screen, 'height',      {{ get: () => {height} }});
    Object.defineProperty(screen, 'availWidth',  {{ get: () => {avail_width} }});
    Object.defineProperty(screen, 'availHeight', {{ get: () => {avail_height} }});
    Object.defineProperty(screen, 'colorDepth',  {{ get: () => {color_depth} }});
    Object.defineProperty(screen, 'pixelDepth',  {{ get: () => {color_depth} }});
    Object.defineProperty(window, 'devicePixelRatio', {{ get: () => {pixel_ratio} }});
    Object.defineProperty(window, 'outerWidth',  {{ get: () => {width} }});
    Object.defineProperty(window, 'outerHeight', {{ get: () => {height} }});
    Object.defineProperty(window, 'innerWidth',  {{ get: () => {avail_width} }});
    Object.defineProperty(window, 'innerHeight', {{ get: () => {avail_height} }});
}})();
"""


def _timezone_script(timezone: str, offset: int | None) -> str:
    """Generate JS to spoof the browser timezone."""
    parts = [f"""
(function() {{
    const TARGET_TZ = "{timezone}";
"""]
    if offset is not None:
        parts.append(f"""
    // Override getTimezoneOffset.
    const _origOffset = Date.prototype.getTimezoneOffset;
    Date.prototype.getTimezoneOffset = function() {{ return {offset}; }};
    Date.prototype.getTimezoneOffset.toString = () => 'function getTimezoneOffset() {{ [native code] }}';
""")

    parts.append(f"""
    // Override Intl.DateTimeFormat resolvedOptions.
    const _origDTF = Intl.DateTimeFormat;
    const _handler = {{
        construct: function(target, args) {{
            const opts = args[1] || {{}};
            if (!opts.timeZone) opts.timeZone = TARGET_TZ;
            args[1] = opts;
            return new target(...args);
        }},
    }};
    Intl.DateTimeFormat = new Proxy(_origDTF, _handler);
    Intl.DateTimeFormat.prototype = _origDTF.prototype;
    Intl.DateTimeFormat.toString = () => 'function DateTimeFormat() {{ [native code] }}';
}})();
""")
    return "".join(parts)


def _hardware_script(concurrency: int, memory: float) -> str:
    """Generate JS to spoof hardware metrics."""
    return f"""
(function() {{
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {concurrency} }});
    Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {memory} }});
}})();
"""


# ── public API ────────────────────────────────────────────────────

async def apply_fingerprint(
    session: CDPSession,
    config: FingerprintConfig | None = None,
) -> FingerprintConfig:
    """Inject fingerprint-spoofing scripts into a CDP session.

    Parameters
    ----------
    session:
        Active CDP session (Page domain must be enabled).
    config:
        Fingerprint parameters.  If ``None``, random realistic
        values are chosen automatically.

    Returns
    -------
    The resolved ``FingerprintConfig`` that was actually applied
    (useful when random values were auto-selected).
    """
    cfg = (config or FingerprintConfig()).resolve()

    scripts: list[tuple[str, str]] = []

    # Canvas noise
    if cfg.canvas_noise:
        scripts.append(("canvas_noise", _canvas_noise_script(cfg.noise_intensity)))

    # WebGL
    if cfg.webgl_vendor and cfg.webgl_renderer:
        scripts.append(("webgl", _webgl_script(cfg.webgl_vendor, cfg.webgl_renderer)))

    # Screen
    if cfg.screen_width and cfg.screen_height:
        scripts.append(("screen", _screen_script(
            cfg.screen_width, cfg.screen_height,
            cfg.avail_width or cfg.screen_width,
            cfg.avail_height or cfg.screen_height - 40,
            cfg.color_depth,
            cfg.pixel_ratio or 1.0,
        )))

    # Timezone
    if cfg.timezone:
        scripts.append(("timezone", _timezone_script(cfg.timezone, cfg.timezone_offset)))
        # Also use CDP Emulation for timezone (belt and suspenders).
        try:
            await session.send("Emulation.setTimezoneOverride", {"timezoneId": cfg.timezone})
        except Exception:
            logger.debug("Emulation.setTimezoneOverride not supported")

    # Hardware
    if cfg.hardware_concurrency or cfg.device_memory:
        scripts.append(("hardware", _hardware_script(
            cfg.hardware_concurrency or 8,
            cfg.device_memory or 8.0,
        )))

    for name, source in scripts:
        await session.send("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        logger.debug("Applied fingerprint script: %s", name)

    logger.info("Fingerprint spoofing applied (GPU=%s, screen=%dx%d)",
                cfg.webgl_renderer, cfg.screen_width or 0, cfg.screen_height or 0)
    return cfg


def random_fingerprint() -> FingerprintConfig:
    """Generate a fully random, realistic fingerprint config."""
    return FingerprintConfig().resolve()
