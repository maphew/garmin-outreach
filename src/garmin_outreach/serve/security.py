"""Security headers, CSP, and the loopback host allowlist for `serve`.

Threat model: loopback is not a privacy boundary (see docs/spec-serve-ui.md
section 8). Any web page the user visits can send cross-origin requests to
localhost, and DNS rebinding lets a malicious page read localhost responses
same-origin. These controls are load-bearing, not hardening backlog.
"""

from __future__ import annotations

import secrets

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Reject any request whose Host header does not resolve to one of these
# (after `_parse_host()` strips the port / bracket syntax and lowercases).
ALLOWED_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

# The vendored Datastar 1.0.2 bundle evaluates data-* expressions with the
# `Function(...)` constructor at runtime (confirmed by grepping
# serve/static/datastar-2837d87a.js for `Function(`), so script-src needs
# 'unsafe-eval'. Every other directive stays maximally restrictive; this is
# the single test-locked header string (docs/spec-serve-ui.md section 8).
#
# worker-src: the vendored MapLibre CSP bundle loads its worker from a
# same-origin URL via `maplibregl.setWorkerUrl(...)` -- that's the whole
# point of the CSP build (verified live in Chromium). `blob:` is only needed
# by the *non-CSP* MapLibre build, which is not what's vendored here.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "script-src 'self' 'unsafe-eval'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data: blob:; "
    "worker-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'"
)

# Applied to every HTTP response, including the ones that bypass the ASGI
# middleware stack entirely (Starlette's ServerErrorMiddleware always wraps
# outermost and calls its 500 handler with the raw, unwrapped `send`).
# `Cache-Control` is the one entry SecurityHeadersMiddleware treats as a
# default rather than an override, since `/static/*` sets its own.
BASELINE_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Cache-Control": "no-store",
}


def _parse_host(raw_host: str) -> str | None:
    """Extract and normalize the hostname portion of a Host header value.

    Handles bracketed IPv6 literals (`[::1]:8477` -> `::1`), bare IPv6
    literals with no brackets or port (`::1` -> `::1`, distinguished from a
    `host:port` pair by having more than one colon), and ordinary `host` /
    `host:port` forms. Returns `None` for empty/missing input.

    Starlette's `TrustedHostMiddleware` instead does a bare
    `host.split(":")[0]`, which turns `[::1]:8477` into `"["` and bare
    `::1` into `""` -- both would then fail every allowlist comparison, so
    `serve` uses this middleware instead.
    """
    text = raw_host.strip()
    if not text:
        return None
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            return None
        suffix = text[end + 1 :]
        if suffix and not _valid_port_suffix(suffix):
            return None
        return text[1:end].lower() or None
    if text.count(":") > 1:
        # A bare IPv6 literal: no brackets, and therefore no port suffix.
        return text.lower()
    host, separator, port = text.partition(":")
    if separator and not _valid_port_suffix(f":{port}"):
        return None
    host = host.strip()
    return host.lower() or None


def _valid_port_suffix(suffix: str) -> bool:
    """Return whether an authority suffix is exactly ``:<ASCII digits>``."""
    if not suffix.startswith(":"):
        return False
    port = suffix[1:]
    return bool(port) and port.isascii() and port.isdigit()


# --- Cross-site request defense (docs/spec-serve-ui.md section 8) ---------
#
# Loopback is not a privacy boundary: any web page the user visits can send
# a cross-origin POST to localhost. Every job-mutating POST route must pass
# all three checks below before its body is even parsed. The custom header
# forces a CORS preflight an attacker's page cannot pass (no permissive CORS
# is ever configured), and its value is a per-process token -- a
# same-origin-only forcing function, not a secret kept out of the page's own
# origin, so it is fine to embed in the dashboard HTML for `jobs.js` to read.

JOB_CSRF_HEADER = "X-Garmin-Outreach-Job"
_ALLOWED_SEC_FETCH_SITE = {"same-origin", "none"}
_CSRF_ERROR_HEADERS = {"Cache-Control": "no-store"}


def new_job_csrf_token() -> str:
    """Mint a per-process job token (docs/spec-serve-ui.md section 8)."""
    return secrets.token_urlsafe(16)


def check_job_csrf(request: Request, token: str) -> JSONResponse | None:
    """Cross-site request defense for POST /api/jobs/*.

    Returns `None` when the request may proceed, or the 403/415 JSON
    response to send otherwise. Order: `Sec-Fetch-Site` (absent header, or
    more than one value, is rejected -- browsers that send it at all send it
    exactly once on every request this app cares about; no permissive
    fallback), then the custom per-process header, then `Content-Type`. Both
    403 causes share one neutral body -- no failure detail is echoed back.
    """
    sec_fetch_site_values = request.headers.getlist("sec-fetch-site")
    if len(sec_fetch_site_values) != 1 or sec_fetch_site_values[0] not in _ALLOWED_SEC_FETCH_SITE:
        return JSONResponse(
            {"error": "request rejected"},
            status_code=403,
            headers=_CSRF_ERROR_HEADERS,
        )
    supplied = request.headers.get(JOB_CSRF_HEADER)
    if not supplied or not supplied.isascii() or not secrets.compare_digest(supplied, token):
        return JSONResponse(
            {"error": "request rejected"},
            status_code=403,
            headers=_CSRF_ERROR_HEADERS,
        )
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse(
            {"error": "unsupported content type"},
            status_code=415,
            headers=_CSRF_ERROR_HEADERS,
        )
    return None


class HostAllowlistMiddleware:
    """DNS-rebinding defense: reject any request whose Host header does not
    resolve to a loopback name (docs/spec-serve-ui.md section 8).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_host = next((value for key, value in scope.get("headers", []) if key == b"host"), None)
        host = _parse_host(raw_host.decode("latin-1")) if raw_host is not None else None
        if host not in ALLOWED_HOSTS:
            response = PlainTextResponse("Invalid host header", status_code=400)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Adds baseline security headers to every HTTP response.

    Every header in `BASELINE_HEADERS` is applied unconditionally except
    `Cache-Control`: the `/static/*` handler sets its own (immutable for
    content-hashed assets, `no-store` for everything else, including static
    404s), and that must survive rather than be overwritten.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in BASELINE_HEADERS.items():
                    if key == "Cache-Control":
                        headers.setdefault(key, value)
                    else:
                        headers[key] = value
            await send(message)

        await self.app(scope, receive, send_wrapper)
