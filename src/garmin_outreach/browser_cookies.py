"""Read a live Garmin session from the user's real browser.

The Explore export lives behind Garmin SSO, which now fronts a fresh automated
sign-in with a Cloudflare bot challenge that a Playwright login cannot reliably
pass. A human who has already signed in with their everyday browser has none of
that friction, so we borrow that browser's ``garmin.com`` cookies and hand them
to the automated context. No password ever passes through this tool, and no
login screen is shown when a valid session already exists.

The pure conversion here (``to_playwright_cookies``) is deliberately kept apart
from the ``rookiepy`` import so it stays unit-testable without a real browser.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

_GARMIN_SUFFIX = "garmin.com"

# Browsers rookiepy can read, most-common first for autodetection.
SUPPORTED_BROWSERS: tuple[str, ...] = (
    "firefox",
    "chrome",
    "edge",
    "brave",
    "chromium",
    "vivaldi",
    "opera",
    "librewolf",
)

_SAME_SITE = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
    "no_restriction": "None",
    "unspecified": None,
    "0": "None",
    "1": "Lax",
    "2": "Strict",
}


def _is_garmin_domain(domain: str) -> bool:
    host = domain.lstrip(".").lower()
    return host == _GARMIN_SUFFIX or host.endswith("." + _GARMIN_SUFFIX)


# Playwright rejects an expiry past year 9999 (seconds). Some backends (e.g.
# rookiepy on Firefox) report milliseconds, which land far beyond that ceiling.
_MAX_EXPIRES_SECONDS = 253402300799


def _expires_seconds(value: Any) -> int | None:
    """Return a positive unix-seconds expiry, or None for a session cookie.

    Values above the year-9999 ceiling are assumed to be milliseconds and scaled
    down; zero, negative, and non-numeric values become session cookies (omitted).
    """
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    seconds = int(value)
    while seconds > _MAX_EXPIRES_SECONDS:
        seconds //= 1000
    return seconds or None


def _normalize_same_site(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip().lower()
    return _SAME_SITE.get(key)


def to_playwright_cookies(raw_cookies: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Convert rookiepy-style cookie mappings to Playwright ``add_cookies`` dicts.

    Only ``garmin.com`` cookies survive, so an unrelated browser session is never
    forwarded to Garmin. Cookies missing a name or value are skipped rather than
    raising, since browser stores occasionally hold partial rows.
    """
    cookies: list[dict] = []
    for raw in raw_cookies:
        name = raw.get("name")
        value = raw.get("value")
        domain = raw.get("domain") or ""
        if not name or value is None or not _is_garmin_domain(domain):
            continue
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": raw.get("path") or "/",
            "secure": bool(raw.get("secure", False)),
            "httpOnly": bool(raw.get("http_only", raw.get("httpOnly", False))),
        }
        expires = _expires_seconds(raw.get("expires"))
        if expires is not None:
            cookie["expires"] = expires
        same_site = _normalize_same_site(raw.get("same_site", raw.get("sameSite")))
        if same_site is not None:
            cookie["sameSite"] = same_site
        cookies.append(cookie)
    return cookies


def _read_raw(rookiepy: Any, browser: str) -> list[Mapping[str, Any]]:
    reader = getattr(rookiepy, browser, None)
    if reader is None:
        raise RuntimeError(
            f"rookiepy cannot read the {browser!r} browser. "
            f"Supported: {', '.join(SUPPORTED_BROWSERS)}."
        )
    # rookiepy filters server-side by domain suffix, cutting decryption work.
    return list(reader([_GARMIN_SUFFIX]))


def load_garmin_cookies(browser: str | None = None) -> list[dict]:
    """Return Playwright-ready Garmin cookies from the user's real browser.

    With ``browser`` set, read exactly that browser and fail loudly if it has no
    Garmin session. With ``browser`` unset, try each supported browser in turn and
    return the first that yields Garmin cookies. Raises ``RuntimeError`` (never a
    bare ImportError) so the caller can degrade to interactive login.
    """
    try:
        import rookiepy
    except ImportError as error:
        raise RuntimeError(
            "Reading saved browser cookies needs the browser extra: `pip install -e .[browser]`."
        ) from error

    if browser:
        cookies = to_playwright_cookies(_read_raw(rookiepy, browser))
        if not cookies:
            raise RuntimeError(
                f"No Garmin session found in {browser}. Sign in to "
                "explore.garmin.com in that browser, then re-run."
            )
        return cookies

    tried: list[str] = []
    for candidate in SUPPORTED_BROWSERS:
        if not hasattr(rookiepy, candidate):
            continue
        tried.append(candidate)
        try:
            cookies = to_playwright_cookies(_read_raw(rookiepy, candidate))
        except Exception:
            # A browser that is not installed (or is locked) raises; keep scanning.
            continue
        if cookies:
            return cookies

    raise RuntimeError(
        "No Garmin session found in any supported browser "
        f"({', '.join(tried) or 'none detected'}). Sign in to explore.garmin.com "
        "in your browser, or pass --browser to name one explicitly."
    )
