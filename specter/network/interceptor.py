"""Network request/response interception using the CDP Fetch domain.

Provides fine-grained control over every HTTP request the browser makes:
  * **Block** requests matching URL patterns.
  * **Modify** request headers before they reach the server.
  * **Mock** responses with custom status/headers/body.
  * **Capture** all traffic for analysis or replay.

The interceptor is designed to be layered on top of any ``CDPSession``.
It does *not* depend on the ``Page`` class so it can also be attached
directly to a raw session for lower-level usage.

Usage::

    interceptor = NetworkInterceptor(cdp_session)
    await interceptor.enable()

    interceptor.block_urls(["*analytics*", "*tracking*"])
    interceptor.on_request(my_request_handler)

    # ... browse normally ...

    await interceptor.disable()
    print(interceptor.captured)   # list of (Request, Response|None)
"""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from specter.core.cdp_client import CDPSession
from specter.core.types import Request, Response

logger = logging.getLogger(__name__)

# Type alias for user-supplied handlers.
RequestHandler = Callable[["InterceptedRequest"], Coroutine[Any, Any, None] | None]
ResponseHandler = Callable[["InterceptedResponse"], Coroutine[Any, Any, None] | None]


# ── intercepted wrappers ─────────────────────────────────────────

@dataclass
class InterceptedRequest:
    """Mutable wrapper around a paused Fetch request.

    The handler must call exactly one of ``continue_request``,
    ``fail_request``, or ``fulfill_request`` to release the pause.
    If no handler acts, the interceptor auto-continues.
    """

    request_id: str
    url: str
    method: str
    headers: dict[str, str]
    post_data: str | None
    resource_type: str
    _session: CDPSession
    _handled: bool = field(default=False, init=False)

    async def continue_request(
        self,
        *,
        url: str | None = None,
        method: str | None = None,
        headers: dict[str, str] | None = None,
        post_data: str | None = None,
    ) -> None:
        """Let the request proceed, optionally with modifications."""
        if self._handled:
            return
        self._handled = True
        params: dict[str, Any] = {"requestId": self.request_id}
        if url:
            params["url"] = url
        if method:
            params["method"] = method
        if headers:
            params["headers"] = [{"name": k, "value": v} for k, v in headers.items()]
        if post_data:
            params["postData"] = base64.b64encode(post_data.encode()).decode()
        await self._session.send("Fetch.continueRequest", params)

    async def fail_request(self, reason: str = "Failed") -> None:
        """Abort the request with a network error reason."""
        if self._handled:
            return
        self._handled = True
        await self._session.send("Fetch.failRequest", {
            "requestId": self.request_id,
            "reason": reason,
        })

    async def fulfill_request(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: str | bytes = "",
    ) -> None:
        """Respond without hitting the network (mock response)."""
        if self._handled:
            return
        self._handled = True
        if isinstance(body, str):
            body_bytes = body.encode()
        else:
            body_bytes = body
        params: dict[str, Any] = {
            "requestId": self.request_id,
            "responseCode": status,
            "body": base64.b64encode(body_bytes).decode(),
        }
        if headers:
            params["responseHeaders"] = [{"name": k, "value": v} for k, v in headers.items()]
        await self._session.send("Fetch.fulfillRequest", params)


@dataclass
class InterceptedResponse:
    """Wrapper for an intercepted response (paused at response stage)."""

    request_id: str
    url: str
    status: int
    headers: dict[str, str]
    _session: CDPSession
    _handled: bool = field(default=False, init=False)

    async def body(self) -> bytes:
        """Fetch the response body from the browser."""
        r = await self._session.send("Fetch.getResponseBody", {
            "requestId": self.request_id,
        })
        data = r.get("body", "")
        if r.get("base64Encoded"):
            return base64.b64decode(data)
        return data.encode()

    async def continue_response(
        self,
        *,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> None:
        """Deliver the response (optionally modified) to the page."""
        if self._handled:
            return
        self._handled = True
        if body is not None or status is not None or headers is not None:
            params: dict[str, Any] = {
                "requestId": self.request_id,
                "responseCode": status or self.status,
            }
            if headers:
                params["responseHeaders"] = [{"name": k, "value": v} for k, v in headers.items()]
            if body is not None:
                params["body"] = base64.b64encode(body).decode()
            await self._session.send("Fetch.fulfillRequest", params)
        else:
            await self._session.send("Fetch.continueResponse", {
                "requestId": self.request_id,
            })


# ── main interceptor ─────────────────────────────────────────────

class NetworkInterceptor:
    """Intercept, inspect, and modify network traffic via CDP Fetch domain.

    Parameters
    ----------
    session:
        An active ``CDPSession`` (one per tab).
    capture:
        If ``True`` (default), every request/response pair is stored in
        ``self.captured`` for later analysis.
    """

    def __init__(self, session: CDPSession, *, capture: bool = True):
        self._cdp = session
        self._capture = capture

        # State
        self._enabled = False
        self._block_patterns: list[str] = []
        self._header_overrides: dict[str, str] = {}
        self._mock_rules: list[dict[str, Any]] = []
        self._request_handlers: list[RequestHandler] = []
        self._response_handlers: list[ResponseHandler] = []

        # Captured traffic
        self.captured: list[tuple[Request, Response | None]] = []
        self._pending_requests: dict[str, Request] = {}

    # ── lifecycle ─────────────────────────────────────────────────

    async def enable(
        self,
        *,
        intercept_responses: bool = False,
        url_patterns: list[str] | None = None,
    ) -> None:
        """Enable the Fetch domain and start intercepting.

        Parameters
        ----------
        intercept_responses:
            If True, also pause at the response stage so handlers can
            inspect/modify response bodies.
        url_patterns:
            Optional list of URL patterns to limit interception to
            (uses CDP's ``RequestPattern``).  If omitted, intercepts
            everything.
        """
        if self._enabled:
            return

        patterns: list[dict[str, Any]] = []
        if url_patterns:
            for p in url_patterns:
                patterns.append({"urlPattern": p, "requestStage": "Request"})
                if intercept_responses:
                    patterns.append({"urlPattern": p, "requestStage": "Response"})
        else:
            patterns.append({"urlPattern": "*", "requestStage": "Request"})
            if intercept_responses:
                patterns.append({"urlPattern": "*", "requestStage": "Response"})

        await self._cdp.send("Fetch.enable", {"patterns": patterns})
        self._cdp.on("Fetch.requestPaused", self._on_request_paused)

        # Also listen to Network events for richer capture data.
        self._cdp.on("Network.responseReceived", self._on_response)

        self._enabled = True
        logger.info("Network interceptor enabled")

    async def disable(self) -> None:
        """Stop intercepting and detach event handlers."""
        if not self._enabled:
            return
        self._cdp.off("Fetch.requestPaused", self._on_request_paused)
        self._cdp.off("Network.responseReceived", self._on_response)
        try:
            await self._cdp.send("Fetch.disable")
        except Exception:
            pass
        self._enabled = False
        logger.info("Network interceptor disabled")

    # ── configuration API ─────────────────────────────────────────

    def block_urls(self, patterns: list[str]) -> None:
        """Block requests whose URL matches any of the given glob patterns.

        Example::

            interceptor.block_urls(["*google-analytics*", "*doubleclick*"])
        """
        self._block_patterns.extend(patterns)

    def set_header_overrides(self, headers: dict[str, str]) -> None:
        """Add or replace headers on every outgoing request.

        Example::

            interceptor.set_header_overrides({"X-Custom": "specter"})
        """
        self._header_overrides.update(headers)

    def mock_response(
        self,
        url_pattern: str,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: str | bytes = "",
    ) -> None:
        """Register a mock: requests matching ``url_pattern`` get a fake response.

        Example::

            interceptor.mock_response(
                "*/api/config",
                body='{"feature_flag": true}',
                headers={"Content-Type": "application/json"},
            )
        """
        self._mock_rules.append({
            "pattern": url_pattern,
            "status": status,
            "headers": headers or {},
            "body": body,
        })

    def on_request(self, handler: RequestHandler) -> None:
        """Register a callback invoked for every intercepted request."""
        self._request_handlers.append(handler)

    def on_response(self, handler: ResponseHandler) -> None:
        """Register a callback invoked for every intercepted response."""
        self._response_handlers.append(handler)

    def clear_captured(self) -> None:
        """Discard all captured traffic."""
        self.captured.clear()
        self._pending_requests.clear()

    # ── internal CDP handlers ─────────────────────────────────────

    async def _on_request_paused(self, params: dict) -> None:
        """Handle Fetch.requestPaused for both request and response stages."""
        request_id: str = params.get("requestId", "")
        response_status = params.get("responseStatusCode")

        if response_status is not None:
            # Response stage
            await self._handle_response_stage(params)
            return

        # Request stage
        url: str = params.get("request", {}).get("url", "")
        method: str = params.get("request", {}).get("method", "GET")
        raw_headers: dict[str, str] = params.get("request", {}).get("headers", {})
        post_data: str | None = params.get("request", {}).get("postData")
        resource_type: str = params.get("resourceType", "")

        # Record for capture
        req_obj = Request(
            id=params.get("networkId", request_id),
            url=url,
            method=method,
            headers=dict(raw_headers),
            post_data=post_data,
            resource_type=resource_type,
            timestamp=time.time(),
        )
        if self._capture:
            self._pending_requests[req_obj.id] = req_obj

        # Check block rules
        for pattern in self._block_patterns:
            if fnmatch.fnmatch(url, pattern):
                logger.debug("Blocked: %s", url)
                try:
                    await self._cdp.send("Fetch.failRequest", {
                        "requestId": request_id,
                        "reason": "BlockedByClient",
                    })
                except Exception:
                    pass
                if self._capture:
                    self.captured.append((req_obj, None))
                    self._pending_requests.pop(req_obj.id, None)
                return

        # Check mock rules
        for rule in self._mock_rules:
            if fnmatch.fnmatch(url, rule["pattern"]):
                logger.debug("Mocked: %s", url)
                body = rule["body"]
                body_bytes = body.encode() if isinstance(body, str) else body
                mock_params: dict[str, Any] = {
                    "requestId": request_id,
                    "responseCode": rule["status"],
                    "body": base64.b64encode(body_bytes).decode(),
                }
                if rule["headers"]:
                    mock_params["responseHeaders"] = [
                        {"name": k, "value": v} for k, v in rule["headers"].items()
                    ]
                await self._cdp.send("Fetch.fulfillRequest", mock_params)
                if self._capture:
                    resp = Response(
                        id=req_obj.id, url=url, status=rule["status"],
                        headers=rule["headers"], mime_type="", body_size=len(body_bytes),
                    )
                    self.captured.append((req_obj, resp))
                    self._pending_requests.pop(req_obj.id, None)
                return

        # Build intercepted request for user handlers
        merged_headers = dict(raw_headers)
        merged_headers.update(self._header_overrides)

        intercepted = InterceptedRequest(
            request_id=request_id,
            url=url,
            method=method,
            headers=merged_headers,
            post_data=post_data,
            resource_type=resource_type,
            _session=self._cdp,
        )

        # Run user handlers
        for handler in self._request_handlers:
            try:
                result = handler(intercepted)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Request handler error for %s", url)

        # If no handler acted, continue with (possibly modified) headers
        if not intercepted._handled:
            continue_params: dict[str, Any] = {"requestId": request_id}
            if self._header_overrides:
                continue_params["headers"] = [
                    {"name": k, "value": v} for k, v in merged_headers.items()
                ]
            await self._cdp.send("Fetch.continueRequest", continue_params)

    async def _handle_response_stage(self, params: dict) -> None:
        """Process a request paused at the response stage."""
        request_id = params.get("requestId", "")
        url = params.get("request", {}).get("url", "")
        status = params.get("responseStatusCode", 0)
        raw_headers = params.get("responseHeaders", [])
        headers = {h["name"]: h["value"] for h in raw_headers} if raw_headers else {}

        intercepted = InterceptedResponse(
            request_id=request_id,
            url=url,
            status=status,
            headers=headers,
            _session=self._cdp,
        )

        for handler in self._response_handlers:
            try:
                result = handler(intercepted)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("Response handler error for %s", url)

        if not intercepted._handled:
            try:
                await self._cdp.send("Fetch.continueResponse", {"requestId": request_id})
            except Exception:
                pass

    def _on_response(self, params: dict) -> None:
        """Network.responseReceived -- complete the capture pair."""
        if not self._capture:
            return
        resp_data = params.get("response", {})
        request_id = params.get("requestId", "")
        req = self._pending_requests.pop(request_id, None)
        if req:
            resp = Response(
                id=request_id,
                url=resp_data.get("url", req.url),
                status=resp_data.get("status", 0),
                headers=resp_data.get("headers", {}),
                mime_type=resp_data.get("mimeType", ""),
                body_size=resp_data.get("encodedDataLength", 0),
            )
            self.captured.append((req, resp))

    # ── convenience ───────────────────────────────────────────────

    def requests_for(self, url_pattern: str) -> list[Request]:
        """Return captured requests whose URL matches a glob pattern."""
        return [
            req for req, _ in self.captured
            if fnmatch.fnmatch(req.url, url_pattern)
        ]

    def responses_for(self, url_pattern: str) -> list[Response]:
        """Return captured responses whose URL matches a glob pattern."""
        return [
            resp for _, resp in self.captured
            if resp and fnmatch.fnmatch(resp.url, url_pattern)
        ]

    @property
    def traffic_summary(self) -> dict[str, int]:
        """Quick breakdown: total requests, by method, by status bucket."""
        summary: dict[str, int] = {"total": len(self.captured)}
        for req, resp in self.captured:
            method_key = f"method_{req.method}"
            summary[method_key] = summary.get(method_key, 0) + 1
            if resp:
                bucket = f"{resp.status // 100}xx"
                summary[bucket] = summary.get(bucket, 0) + 1
        return summary
