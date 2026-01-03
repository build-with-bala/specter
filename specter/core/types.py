"""Shared type definitions for the entire Specter package.

This module is the single source of truth for all data structures.
Every other module imports from here — never the reverse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Enums ──────────────────────────────────────────────────────────

class MouseButton(Enum):
    LEFT = "left"
    RIGHT = "right"
    MIDDLE = "middle"


class KeyModifier(Enum):
    ALT = 1
    CTRL = 2
    META = 4
    SHIFT = 8


class ResourceType(Enum):
    DOCUMENT = "Document"
    STYLESHEET = "Stylesheet"
    IMAGE = "Image"
    MEDIA = "Media"
    FONT = "Font"
    SCRIPT = "Script"
    XHR = "XHR"
    FETCH = "Fetch"
    WEBSOCKET = "WebSocket"
    OTHER = "Other"


class WaitUntil(Enum):
    LOAD = "load"
    DOMCONTENTLOADED = "domcontentloaded"
    NETWORKIDLE = "networkidle"


# ── Geometry ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class Box:
    x: float
    y: float
    width: float
    height: float

    @property
    def center(self) -> Point:
        return Point(self.x + self.width / 2, self.y + self.height / 2)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x <= self.x + self.width and self.y <= p.y <= self.y + self.height


# ── DOM ────────────────────────────────────────────────────────────

@dataclass
class ElementHandle:
    """Lightweight reference to a DOM node held by the browser."""
    node_id: int
    backend_node_id: int
    object_id: str = ""
    tag: str = ""
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    box: Box | None = None
    visible: bool = True
    enabled: bool = True
    role: str = ""        # ARIA role
    name: str = ""        # Accessible name

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    @property
    def css_selector(self) -> str:
        if self.id:
            return f"#{self.id}"
        parts = [self.tag.lower()]
        for c in self.classes[:2]:
            parts.append(f".{c}")
        return "".join(parts)


# ── Network ────────────────────────────────────────────────────────

@dataclass
class Request:
    id: str
    url: str
    method: str
    headers: dict[str, str] = field(default_factory=dict)
    post_data: str | None = None
    resource_type: str = ""
    timestamp: float = 0.0


@dataclass
class Response:
    id: str
    url: str
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    mime_type: str = ""
    body_size: int = 0


# ── Cookie ─────────────────────────────────────────────────────────

@dataclass
class Cookie:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: float = -1
    http_only: bool = False
    secure: bool = False
    same_site: str = "Lax"

    def to_cdp_param(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "name": self.name, "value": self.value,
            "domain": self.domain, "path": self.path,
            "httpOnly": self.http_only, "secure": self.secure,
            "sameSite": self.same_site,
        }
        if self.expires > 0:
            p["expires"] = self.expires
        return p


# ── Actions (for recording / replay) ──────────────────────────────

@dataclass
class RecordedAction:
    kind: str                           # click, type, navigate, scroll, wait, ...
    timestamp: float = 0.0
    selector: str = ""
    url: str = ""
    text: str = ""
    key: str = ""
    x: float = 0.0
    y: float = 0.0
    scroll_x: int = 0
    scroll_y: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
