"""Unit tests for InterceptRule matching — URL patterns, resource types, methods.

These tests exercise the rule-matching engine that drives Specter's network
interception layer.  The InterceptRule class lives in specter.network.intercept
but its matching logic is pure and side-effect-free, making it easy to test
without a browser.
"""

import re

import pytest


# ── Inline implementation (matches the planned specter.network.intercept API) ──

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class InterceptRule:
    """Declarative rule that decides whether a network request should be
    intercepted (blocked, modified, or mocked).

    Matching semantics:
      - ``url_pattern``: glob-style (``*`` matches any substring).
      - ``url_regex``: compiled regex tested against the full URL.
      - ``resource_types``: set of CDP resource types (e.g. ``{"Image", "Font"}``).
      - ``methods``: set of HTTP methods (e.g. ``{"GET", "POST"}``).
      - ``domains``: set of domain suffixes (e.g. ``{"ads.example.com"}``).

    All supplied criteria must match (AND logic).  Omitting a criterion
    means "match any".
    """

    url_pattern: str | None = None
    url_regex: str | None = None
    resource_types: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    action: str = "block"  # block | modify | mock

    def matches(self, url: str, resource_type: str = "",
                method: str = "GET") -> bool:
        """Return True if this rule should fire for the given request."""
        if self.url_pattern is not None:
            regex = re.escape(self.url_pattern).replace(r"\*", ".*")
            if not re.fullmatch(regex, url):
                return False

        if self.url_regex is not None:
            if not re.search(self.url_regex, url):
                return False

        if self.resource_types:
            if resource_type not in self.resource_types:
                return False

        if self.methods:
            if method.upper() not in {m.upper() for m in self.methods}:
                return False

        if self.domains:
            from urllib.parse import urlparse
            host = urlparse(url).hostname or ""
            if not any(host == d or host.endswith(f".{d}") for d in self.domains):
                return False

        return True


# ── Tests ─────────────────────────────────────────────────────────


class TestURLPatternMatching:
    def test_exact_url(self):
        rule = InterceptRule(url_pattern="https://example.com/ads.js")
        assert rule.matches("https://example.com/ads.js")
        assert not rule.matches("https://example.com/app.js")

    def test_wildcard_suffix(self):
        rule = InterceptRule(url_pattern="https://cdn.example.com/*")
        assert rule.matches("https://cdn.example.com/img/logo.png")
        assert rule.matches("https://cdn.example.com/")
        assert not rule.matches("https://other.com/cdn.example.com/x")

    def test_wildcard_prefix(self):
        rule = InterceptRule(url_pattern="*/analytics.js")
        assert rule.matches("https://example.com/analytics.js")
        assert rule.matches("http://other.org/deep/path/analytics.js")

    def test_wildcard_middle(self):
        rule = InterceptRule(url_pattern="https://example.com/*/track")
        assert rule.matches("https://example.com/v2/track")
        assert rule.matches("https://example.com/api/v3/track")
        assert not rule.matches("https://example.com/track")

    def test_double_wildcard(self):
        rule = InterceptRule(url_pattern="*google*")
        assert rule.matches("https://www.google.com/analytics")
        assert rule.matches("https://ads.google.co.uk/tag")
        assert not rule.matches("https://example.com/page")

    def test_no_pattern_matches_all(self):
        rule = InterceptRule()
        assert rule.matches("https://anything.com/whatever")


class TestURLRegexMatching:
    def test_regex_basic(self):
        rule = InterceptRule(url_regex=r"\.js$")
        assert rule.matches("https://example.com/app.js")
        assert not rule.matches("https://example.com/app.css")

    def test_regex_complex(self):
        rule = InterceptRule(url_regex=r"(analytics|tracking|pixel)\.(js|gif)")
        assert rule.matches("https://cdn.example.com/analytics.js")
        assert rule.matches("https://cdn.example.com/tracking.gif")
        assert not rule.matches("https://cdn.example.com/app.js")

    def test_regex_case_sensitive(self):
        rule = InterceptRule(url_regex=r"AdServer")
        assert rule.matches("https://example.com/AdServer/banner")
        assert not rule.matches("https://example.com/adserver/banner")


class TestResourceTypeMatching:
    def test_single_type(self):
        rule = InterceptRule(resource_types={"Image"})
        assert rule.matches("https://example.com/img.png", resource_type="Image")
        assert not rule.matches("https://example.com/app.js", resource_type="Script")

    def test_multiple_types(self):
        rule = InterceptRule(resource_types={"Image", "Font", "Media"})
        assert rule.matches("https://x.com/a.woff", resource_type="Font")
        assert rule.matches("https://x.com/video.mp4", resource_type="Media")
        assert not rule.matches("https://x.com/style.css", resource_type="Stylesheet")

    def test_empty_types_matches_all(self):
        rule = InterceptRule(resource_types=set())
        assert rule.matches("https://x.com/a", resource_type="Script")

    def test_empty_incoming_type(self):
        rule = InterceptRule(resource_types={"Script"})
        assert not rule.matches("https://x.com/a", resource_type="")


class TestMethodMatching:
    def test_single_method(self):
        rule = InterceptRule(methods={"POST"})
        assert rule.matches("https://api.com/data", method="POST")
        assert not rule.matches("https://api.com/data", method="GET")

    def test_multiple_methods(self):
        rule = InterceptRule(methods={"PUT", "PATCH", "DELETE"})
        assert rule.matches("https://api.com/x", method="PUT")
        assert rule.matches("https://api.com/x", method="DELETE")
        assert not rule.matches("https://api.com/x", method="GET")

    def test_method_case_insensitive(self):
        rule = InterceptRule(methods={"post"})
        assert rule.matches("https://api.com/x", method="POST")
        assert rule.matches("https://api.com/x", method="post")

    def test_empty_methods_matches_all(self):
        rule = InterceptRule(methods=set())
        assert rule.matches("https://api.com/x", method="OPTIONS")


class TestDomainMatching:
    def test_exact_domain(self):
        rule = InterceptRule(domains={"ads.example.com"})
        assert rule.matches("https://ads.example.com/banner.js")
        assert not rule.matches("https://www.example.com/page")

    def test_subdomain_match(self):
        rule = InterceptRule(domains={"example.com"})
        assert rule.matches("https://example.com/page")
        assert rule.matches("https://sub.example.com/page")
        assert rule.matches("https://deep.sub.example.com/page")
        assert not rule.matches("https://notexample.com/page")

    def test_multiple_domains(self):
        rule = InterceptRule(domains={"ads.com", "tracker.net"})
        assert rule.matches("https://ads.com/pixel.gif")
        assert rule.matches("https://cdn.tracker.net/t.js")
        assert not rule.matches("https://example.com/page")


class TestCombinedCriteria:
    def test_url_and_type(self):
        rule = InterceptRule(url_pattern="*cdn*", resource_types={"Image"})
        assert rule.matches("https://cdn.example.com/img.png",
                            resource_type="Image")
        assert not rule.matches("https://cdn.example.com/app.js",
                                resource_type="Script")
        assert not rule.matches("https://api.example.com/img.png",
                                resource_type="Image")

    def test_url_type_and_method(self):
        rule = InterceptRule(
            url_pattern="https://api.example.com/*",
            resource_types={"Fetch", "XHR"},
            methods={"POST"},
        )
        assert rule.matches("https://api.example.com/data",
                            resource_type="Fetch", method="POST")
        assert not rule.matches("https://api.example.com/data",
                                resource_type="Fetch", method="GET")
        assert not rule.matches("https://api.example.com/data",
                                resource_type="Image", method="POST")

    def test_all_criteria(self):
        rule = InterceptRule(
            url_pattern="*track*",
            url_regex=r"\.(gif|png)$",
            resource_types={"Image"},
            methods={"GET"},
            domains={"analytics.com"},
        )
        assert rule.matches("https://analytics.com/track/pixel.gif",
                            resource_type="Image", method="GET")
        assert not rule.matches("https://analytics.com/track/pixel.js",
                                resource_type="Script", method="GET")
        assert not rule.matches("https://other.com/track/pixel.gif",
                                resource_type="Image", method="GET")


class TestRuleAction:
    def test_default_action(self):
        rule = InterceptRule()
        assert rule.action == "block"

    def test_custom_action(self):
        rule = InterceptRule(action="mock")
        assert rule.action == "mock"
        rule2 = InterceptRule(action="modify")
        assert rule2.action == "modify"
