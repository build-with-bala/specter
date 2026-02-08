"""Unit tests for wait strategy factory functions.

These tests verify the creation and configuration of wait strategies
used by Specter to determine when a page operation is "done" — e.g.
wait for a selector, wait for network idle, wait for a JS predicate.
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

import pytest


# ── Inline implementation (matches planned specter.core.waits API) ──


class WaitKind(Enum):
    SELECTOR = "selector"
    XPATH = "xpath"
    FUNCTION = "function"
    NAVIGATION = "navigation"
    NETWORK_IDLE = "network_idle"
    TIMEOUT = "timeout"
    URL = "url"


@dataclass
class WaitStrategy:
    """A declarative wait condition produced by factory helpers."""
    kind: WaitKind
    value: str = ""
    timeout: float = 30.0
    visible: bool = True
    poll_interval: float = 0.15
    idle_time: float = 0.5
    url_pattern: str = ""
    options: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> str:
        """Human-readable summary of what this wait does."""
        if self.kind == WaitKind.SELECTOR:
            vis = " (visible)" if self.visible else ""
            return f"wait for selector '{self.value}'{vis}, timeout={self.timeout}s"
        if self.kind == WaitKind.XPATH:
            return f"wait for xpath '{self.value}', timeout={self.timeout}s"
        if self.kind == WaitKind.FUNCTION:
            return f"wait for function, timeout={self.timeout}s"
        if self.kind == WaitKind.NAVIGATION:
            return f"wait for navigation, timeout={self.timeout}s"
        if self.kind == WaitKind.NETWORK_IDLE:
            return f"wait for network idle ({self.idle_time}s), timeout={self.timeout}s"
        if self.kind == WaitKind.TIMEOUT:
            return f"wait {self.timeout}s"
        if self.kind == WaitKind.URL:
            return f"wait for url matching '{self.url_pattern}', timeout={self.timeout}s"
        return f"wait ({self.kind.value})"


# ── Factory functions ─────────────────────────────────────────────


def wait_for_selector(selector: str, *, timeout: float = 30.0,
                      visible: bool = True) -> WaitStrategy:
    """Wait until a CSS selector matches an element in the DOM."""
    return WaitStrategy(
        kind=WaitKind.SELECTOR,
        value=selector,
        timeout=timeout,
        visible=visible,
    )


def wait_for_xpath(expression: str, *, timeout: float = 30.0) -> WaitStrategy:
    """Wait until an XPath expression matches."""
    return WaitStrategy(
        kind=WaitKind.XPATH,
        value=expression,
        timeout=timeout,
    )


def wait_for_function(expression: str, *, timeout: float = 10.0,
                      poll_interval: float = 0.1) -> WaitStrategy:
    """Wait until a JS expression evaluates to truthy."""
    return WaitStrategy(
        kind=WaitKind.FUNCTION,
        value=expression,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def wait_for_navigation(*, timeout: float = 30.0) -> WaitStrategy:
    """Wait for the next page navigation event."""
    return WaitStrategy(kind=WaitKind.NAVIGATION, timeout=timeout)


def wait_for_network_idle(*, idle_time: float = 0.5,
                          timeout: float = 30.0) -> WaitStrategy:
    """Wait until no network requests are in flight for *idle_time* seconds."""
    return WaitStrategy(
        kind=WaitKind.NETWORK_IDLE,
        timeout=timeout,
        idle_time=idle_time,
    )


def wait_for_timeout(seconds: float) -> WaitStrategy:
    """Unconditional delay."""
    return WaitStrategy(kind=WaitKind.TIMEOUT, timeout=seconds)


def wait_for_url(pattern: str, *, timeout: float = 30.0) -> WaitStrategy:
    """Wait until the page URL matches a glob pattern."""
    return WaitStrategy(
        kind=WaitKind.URL,
        url_pattern=pattern,
        timeout=timeout,
    )


# ── Tests ─────────────────────────────────────────────────────────


class TestWaitForSelector:
    def test_defaults(self):
        w = wait_for_selector("#login-form")
        assert w.kind == WaitKind.SELECTOR
        assert w.value == "#login-form"
        assert w.timeout == 30.0
        assert w.visible is True

    def test_custom_timeout(self):
        w = wait_for_selector(".modal", timeout=5.0)
        assert w.timeout == 5.0

    def test_hidden_element(self):
        w = wait_for_selector("[data-loading]", visible=False)
        assert w.visible is False

    def test_describe(self):
        w = wait_for_selector("#btn")
        desc = w.describe()
        assert "selector" in desc
        assert "#btn" in desc
        assert "visible" in desc

    def test_describe_not_visible(self):
        w = wait_for_selector("#x", visible=False)
        assert "visible" not in w.describe()


class TestWaitForXpath:
    def test_defaults(self):
        w = wait_for_xpath("//div[@class='result']")
        assert w.kind == WaitKind.XPATH
        assert w.value == "//div[@class='result']"
        assert w.timeout == 30.0

    def test_custom_timeout(self):
        w = wait_for_xpath("//a", timeout=2.0)
        assert w.timeout == 2.0

    def test_describe(self):
        w = wait_for_xpath("//span")
        assert "xpath" in w.describe()


class TestWaitForFunction:
    def test_defaults(self):
        w = wait_for_function("window.appReady === true")
        assert w.kind == WaitKind.FUNCTION
        assert w.value == "window.appReady === true"
        assert w.timeout == 10.0
        assert w.poll_interval == 0.1

    def test_custom_poll(self):
        w = wait_for_function("true", poll_interval=0.5)
        assert w.poll_interval == 0.5

    def test_describe(self):
        w = wait_for_function("document.ready")
        assert "function" in w.describe()


class TestWaitForNavigation:
    def test_defaults(self):
        w = wait_for_navigation()
        assert w.kind == WaitKind.NAVIGATION
        assert w.timeout == 30.0

    def test_custom_timeout(self):
        w = wait_for_navigation(timeout=60.0)
        assert w.timeout == 60.0

    def test_describe(self):
        w = wait_for_navigation()
        assert "navigation" in w.describe()


class TestWaitForNetworkIdle:
    def test_defaults(self):
        w = wait_for_network_idle()
        assert w.kind == WaitKind.NETWORK_IDLE
        assert w.timeout == 30.0
        assert w.idle_time == 0.5

    def test_custom_idle(self):
        w = wait_for_network_idle(idle_time=2.0, timeout=10.0)
        assert w.idle_time == 2.0
        assert w.timeout == 10.0

    def test_describe(self):
        w = wait_for_network_idle(idle_time=1.0)
        assert "network idle" in w.describe()
        assert "1.0s" in w.describe()


class TestWaitForTimeout:
    def test_basic(self):
        w = wait_for_timeout(3.0)
        assert w.kind == WaitKind.TIMEOUT
        assert w.timeout == 3.0

    def test_describe(self):
        w = wait_for_timeout(5.0)
        assert "5.0s" in w.describe()


class TestWaitForURL:
    def test_basic(self):
        w = wait_for_url("*/dashboard*")
        assert w.kind == WaitKind.URL
        assert w.url_pattern == "*/dashboard*"
        assert w.timeout == 30.0

    def test_custom_timeout(self):
        w = wait_for_url("https://example.com/done", timeout=15.0)
        assert w.timeout == 15.0

    def test_describe(self):
        w = wait_for_url("*/success*")
        assert "url" in w.describe()
        assert "success" in w.describe()


class TestWaitStrategyOptions:
    def test_default_options_empty(self):
        w = wait_for_selector("div")
        assert w.options == {}

    def test_options_isolation(self):
        w1 = wait_for_selector("a")
        w2 = wait_for_selector("b")
        w1.options["key"] = "val"
        assert "key" not in w2.options


class TestWaitKindEnum:
    def test_all_values(self):
        expected = {"selector", "xpath", "function", "navigation",
                    "network_idle", "timeout", "url"}
        assert {k.value for k in WaitKind} == expected
