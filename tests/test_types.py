"""Unit tests for specter.core.types — geometry, DOM handles, cookies, actions."""

import pytest

from specter.core.types import (
    Box,
    Cookie,
    ElementHandle,
    KeyModifier,
    MouseButton,
    Point,
    RecordedAction,
    ResourceType,
    WaitUntil,
)


# ── Point ─────────────────────────────────────────────────────────


class TestPoint:
    def test_creation(self):
        p = Point(10.5, 20.3)
        assert p.x == 10.5
        assert p.y == 20.3

    def test_frozen(self):
        p = Point(1, 2)
        with pytest.raises(AttributeError):
            p.x = 99  # type: ignore[misc]

    def test_equality(self):
        assert Point(3, 4) == Point(3, 4)
        assert Point(3, 4) != Point(4, 3)

    def test_hash(self):
        s = {Point(1, 2), Point(1, 2), Point(3, 4)}
        assert len(s) == 2


# ── Box ───────────────────────────────────────────────────────────


class TestBox:
    def test_center_basic(self):
        b = Box(x=0, y=0, width=100, height=50)
        c = b.center
        assert c.x == 50.0
        assert c.y == 25.0

    def test_center_offset(self):
        b = Box(x=10, y=20, width=60, height=40)
        c = b.center
        assert c.x == 40.0
        assert c.y == 40.0

    def test_center_zero_size(self):
        b = Box(x=5, y=5, width=0, height=0)
        assert b.center == Point(5, 5)

    def test_contains_inside(self):
        b = Box(0, 0, 100, 100)
        assert b.contains(Point(50, 50))

    def test_contains_on_edge(self):
        b = Box(0, 0, 100, 100)
        assert b.contains(Point(0, 0))
        assert b.contains(Point(100, 100))
        assert b.contains(Point(0, 100))
        assert b.contains(Point(100, 0))

    def test_contains_outside(self):
        b = Box(10, 10, 50, 50)
        assert not b.contains(Point(0, 0))
        assert not b.contains(Point(100, 100))
        assert not b.contains(Point(9.99, 30))
        assert not b.contains(Point(30, 60.01))

    def test_contains_offset_box(self):
        b = Box(100, 200, 50, 30)
        assert b.contains(Point(125, 215))
        assert not b.contains(Point(99, 200))
        assert not b.contains(Point(151, 200))

    def test_frozen(self):
        b = Box(0, 0, 10, 10)
        with pytest.raises(AttributeError):
            b.x = 99  # type: ignore[misc]


# ── ElementHandle ─────────────────────────────────────────────────


class TestElementHandle:
    def _make(self, **kw):
        defaults = dict(node_id=1, backend_node_id=2)
        defaults.update(kw)
        return ElementHandle(**defaults)

    def test_id_from_attrs(self):
        el = self._make(attrs={"id": "main-form", "class": "form big"})
        assert el.id == "main-form"

    def test_id_empty_when_missing(self):
        el = self._make(attrs={"class": "foo"})
        assert el.id == ""

    def test_classes_split(self):
        el = self._make(attrs={"class": "btn btn-primary large"})
        assert el.classes == ["btn", "btn-primary", "large"]

    def test_classes_empty(self):
        el = self._make(attrs={})
        assert el.classes == []  # "".split() returns []

    def test_css_selector_with_id(self):
        el = self._make(tag="DIV", attrs={"id": "sidebar"})
        assert el.css_selector == "#sidebar"

    def test_css_selector_tag_only(self):
        el = self._make(tag="SPAN", attrs={})
        assert el.css_selector == "span"

    def test_css_selector_tag_with_classes(self):
        el = self._make(tag="BUTTON", attrs={"class": "btn primary submit"})
        assert el.css_selector == "button.btn.primary"

    def test_css_selector_single_class(self):
        el = self._make(tag="A", attrs={"class": "link"})
        assert el.css_selector == "a.link"

    def test_css_selector_id_takes_priority_over_classes(self):
        el = self._make(tag="DIV", attrs={"id": "nav", "class": "main wide"})
        assert el.css_selector == "#nav"

    def test_defaults(self):
        el = self._make()
        assert el.text == ""
        assert el.box is None
        assert el.visible is True
        assert el.enabled is True
        assert el.role == ""
        assert el.name == ""


# ── Cookie ────────────────────────────────────────────────────────


class TestCookie:
    def test_to_cdp_param_basic(self):
        c = Cookie(name="session", value="abc123", domain=".example.com")
        p = c.to_cdp_param()
        assert p["name"] == "session"
        assert p["value"] == "abc123"
        assert p["domain"] == ".example.com"
        assert p["path"] == "/"
        assert p["httpOnly"] is False
        assert p["secure"] is False
        assert p["sameSite"] == "Lax"
        assert "expires" not in p

    def test_to_cdp_param_with_expires(self):
        c = Cookie(name="token", value="x", expires=1700000000.0)
        p = c.to_cdp_param()
        assert p["expires"] == 1700000000.0

    def test_to_cdp_param_no_expires_when_negative(self):
        c = Cookie(name="a", value="b", expires=-1)
        assert "expires" not in c.to_cdp_param()

    def test_to_cdp_param_no_expires_when_zero(self):
        c = Cookie(name="a", value="b", expires=0)
        assert "expires" not in c.to_cdp_param()

    def test_to_cdp_param_secure_httponly(self):
        c = Cookie(name="s", value="v", secure=True, http_only=True,
                   same_site="Strict")
        p = c.to_cdp_param()
        assert p["secure"] is True
        assert p["httpOnly"] is True
        assert p["sameSite"] == "Strict"

    def test_defaults(self):
        c = Cookie(name="n", value="v")
        assert c.domain == ""
        assert c.path == "/"
        assert c.expires == -1
        assert c.http_only is False
        assert c.secure is False
        assert c.same_site == "Lax"


# ── RecordedAction ────────────────────────────────────────────────


class TestRecordedAction:
    def test_minimal(self):
        a = RecordedAction(kind="click")
        assert a.kind == "click"
        assert a.timestamp == 0.0
        assert a.selector == ""
        assert a.url == ""

    def test_click_action(self):
        a = RecordedAction(kind="click", selector="#btn", x=100.0, y=200.0,
                           timestamp=1234567890.0)
        assert a.kind == "click"
        assert a.selector == "#btn"
        assert a.x == 100.0
        assert a.y == 200.0

    def test_type_action(self):
        a = RecordedAction(kind="type", selector="input.search",
                           text="hello world")
        assert a.text == "hello world"

    def test_navigate_action(self):
        a = RecordedAction(kind="navigate", url="https://example.com")
        assert a.url == "https://example.com"

    def test_scroll_action(self):
        a = RecordedAction(kind="scroll", scroll_x=0, scroll_y=300)
        assert a.scroll_y == 300

    def test_meta_dict(self):
        a = RecordedAction(kind="click", meta={"button": "right"})
        assert a.meta["button"] == "right"

    def test_meta_default_empty(self):
        a1 = RecordedAction(kind="a")
        a2 = RecordedAction(kind="b")
        a1.meta["key"] = "val"
        assert "key" not in a2.meta  # no shared default dict


# ── Enums ─────────────────────────────────────────────────────────


class TestEnums:
    def test_mouse_button_values(self):
        assert MouseButton.LEFT.value == "left"
        assert MouseButton.RIGHT.value == "right"
        assert MouseButton.MIDDLE.value == "middle"

    def test_key_modifier_values(self):
        assert KeyModifier.ALT.value == 1
        assert KeyModifier.CTRL.value == 2
        assert KeyModifier.META.value == 4
        assert KeyModifier.SHIFT.value == 8

    def test_resource_type_values(self):
        assert ResourceType.DOCUMENT.value == "Document"
        assert ResourceType.FETCH.value == "Fetch"
        assert ResourceType.WEBSOCKET.value == "WebSocket"

    def test_wait_until_values(self):
        assert WaitUntil.LOAD.value == "load"
        assert WaitUntil.DOMCONTENTLOADED.value == "domcontentloaded"
        assert WaitUntil.NETWORKIDLE.value == "networkidle"
