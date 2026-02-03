"""YAML configuration loader for Specter.

Loads settings from ``config/default.yaml`` (shipped with the package),
then overlays ``config/local.yaml`` if present, then environment variables.

Usage::

    from specter.config import load_config
    cfg = load_config()
    print(cfg.browser.headless)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ── Paths ─────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG = _PROJECT_ROOT / "config" / "default.yaml"
_LOCAL_CONFIG = _PROJECT_ROOT / "config" / "local.yaml"


# ── Nested config dataclasses ─────────────────────────────────────


@dataclass
class BrowserSettings:
    headless: bool = True
    debug_port: int = 9222
    window_width: int = 1920
    window_height: int = 1080
    locale: str = "en-US"
    timezone: str | None = None
    proxy: str | None = None
    persist_profile: bool = False
    user_data_dir: str | None = None
    extra_args: list[str] = field(default_factory=list)
    launch_timeout: float = 15.0


@dataclass
class HumanInputSettings:
    enabled: bool = True
    mouse_bezier_steps: int = 20
    mouse_jitter: float = 1.5
    typing_wpm: float = 80
    typing_variance: float = 0.4
    click_delay_ms: list[int] = field(default_factory=lambda: [40, 120])
    scroll_step_px: list[int] = field(default_factory=lambda: [80, 250])


@dataclass
class EvasionSettings:
    webdriver: bool = True
    chrome_runtime: bool = True
    permissions: bool = True
    plugins: bool = True
    languages: bool = True
    webgl_vendor: bool = True
    hairline: bool = True
    media_codecs: bool = True
    canvas_fingerprint: bool = True


@dataclass
class StealthSettings:
    enabled: bool = False
    user_agent: str | None = None
    rotate_user_agent: bool = True
    evasions: EvasionSettings = field(default_factory=EvasionSettings)
    human_input: HumanInputSettings = field(default_factory=HumanInputSettings)


@dataclass
class NetworkSettings:
    request_timeout: float = 30.0
    navigation_timeout: float = 30.0
    network_idle_time: float = 0.5
    max_concurrent_requests: int = 50
    block_resource_types: list[str] = field(default_factory=list)
    intercept_patterns: list[dict[str, Any]] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    cookies: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ProviderSettings:
    model: str = ""
    api_key: str | None = None
    base_url: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096


@dataclass
class VisionSettings:
    enabled: bool = True
    screenshot_quality: int = 80
    max_resolution: list[int] = field(default_factory=lambda: [1280, 720])


@dataclass
class ExtractionSettings:
    retries: int = 2
    chunk_size: int = 4000


@dataclass
class AISettings:
    default_provider: str = "openai"
    providers: dict[str, ProviderSettings] = field(default_factory=dict)
    vision: VisionSettings = field(default_factory=VisionSettings)
    extraction: ExtractionSettings = field(default_factory=ExtractionSettings)


@dataclass
class RecordingSettings:
    output_dir: str = "recordings"
    include_screenshots: bool = False
    screenshot_interval: float = 0.0
    include_network: bool = False


@dataclass
class LoggingSettings:
    level: str = "INFO"
    file: str | None = None
    format: str = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"


@dataclass
class Config:
    """Top-level Specter configuration."""
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    stealth: StealthSettings = field(default_factory=StealthSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    ai: AISettings = field(default_factory=AISettings)
    recording: RecordingSettings = field(default_factory=RecordingSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)


# ── Loader helpers ────────────────────────────────────────────────


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into *base* (mutates base)."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def _dict_to_dataclass(cls: type, data: dict) -> Any:
    """Recursively instantiate nested dataclasses from a dict tree."""
    from dataclasses import fields as dc_fields
    kwargs: dict[str, Any] = {}
    known_fields = {f.name: f for f in dc_fields(cls)}
    for name, fld in known_fields.items():
        if name not in data:
            continue
        val = data[name]
        origin = getattr(fld.type, "__origin__", None)
        # Check if the field type is a dataclass
        ftype = fld.type
        if isinstance(ftype, str):
            # Resolve forward references from the current module
            ftype = globals().get(ftype, ftype)
        if isinstance(ftype, type) and hasattr(ftype, "__dataclass_fields__"):
            if isinstance(val, dict):
                kwargs[name] = _dict_to_dataclass(ftype, val)
            else:
                kwargs[name] = val
        elif name == "providers" and isinstance(val, dict):
            # Special case: providers is dict[str, ProviderSettings]
            kwargs[name] = {
                k: _dict_to_dataclass(ProviderSettings, v)
                if isinstance(v, dict) else v
                for k, v in val.items()
            }
        else:
            kwargs[name] = val
    return cls(**kwargs)


def _apply_env_overrides(raw: dict) -> None:
    """Patch config dict with well-known environment variables."""
    env_map = {
        "SPECTER_HEADLESS": ("browser", "headless", lambda v: v.lower() in ("1", "true", "yes")),
        "SPECTER_DEBUG_PORT": ("browser", "debug_port", int),
        "SPECTER_PROXY": ("browser", "proxy", str),
        "SPECTER_STEALTH": ("stealth", "enabled", lambda v: v.lower() in ("1", "true", "yes")),
        "SPECTER_LOG_LEVEL": ("logging", "level", str),
        "OPENAI_API_KEY": ("ai", "providers", "openai", "api_key"),
        "ANTHROPIC_API_KEY": ("ai", "providers", "anthropic", "api_key"),
    }
    for env_var, path_spec in env_map.items():
        val = os.environ.get(env_var)
        if val is None:
            continue
        if callable(path_spec[-1]) and not isinstance(path_spec[-1], str):
            *path, transform = path_spec
            val = transform(val)
        else:
            path = list(path_spec)

        node = raw
        for segment in path[:-1]:
            node = node.setdefault(segment, {})
        node[path[-1]] = val


# ── Public API ────────────────────────────────────────────────────


def load_config(path: str | Path | None = None) -> Config:
    """Load and return a ``Config`` object.

    Resolution order:
        1. ``config/default.yaml`` (package defaults)
        2. ``config/local.yaml`` (user overrides — gitignored)
        3. A custom *path* if provided
        4. Environment variable overrides
    """
    raw: dict[str, Any] = {}

    # 1. defaults
    if _DEFAULT_CONFIG.is_file():
        with open(_DEFAULT_CONFIG) as f:
            raw = yaml.safe_load(f) or {}

    # 2. local overrides
    if _LOCAL_CONFIG.is_file():
        with open(_LOCAL_CONFIG) as f:
            local = yaml.safe_load(f) or {}
            _deep_merge(raw, local)

    # 3. explicit path
    if path:
        p = Path(path)
        if p.is_file():
            with open(p) as f:
                extra = yaml.safe_load(f) or {}
                _deep_merge(raw, extra)

    # 4. env vars
    _apply_env_overrides(raw)

    return _dict_to_dataclass(Config, raw)
