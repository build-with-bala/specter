"""Core browser control — CDP client, browser lifecycle, page interaction."""

from specter.core.cdp_client import CDPSession, CDPClient
from specter.core.browser import Browser, BrowserConfig
from specter.core.page import Page
from specter.core.element import Element

__all__ = ["CDPSession", "CDPClient", "Browser", "BrowserConfig", "Page", "Element"]
