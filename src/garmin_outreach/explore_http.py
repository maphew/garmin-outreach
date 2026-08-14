"""Browserless Garmin Explore export over plain HTTP.

The consumer Explore export is driven by ``/bundles/map.js`` calling two app
endpoints on ``explore.garmin.com``. When the user already has a live session in
their everyday browser we can borrow those cookies (see :mod:`browser_cookies`)
and call the endpoints directly, with no automated browser for Cloudflare to
challenge. This is the default path because a headless browser cannot pass the
Garmin SSO Cloudflare check, while a plain cookied request can, as long as the
account is not rate-limited.

Request contract, reverse-engineered from ``map.js`` (``exportData``):

    POST /Configuration/User/GetUsersSimplified   {includeGroups: true}
        -> the account's users, each carrying an id, group id, and checked flag.

    POST /Map/GetDeviceListForDownload?fileType=KML|GPX   (form-encoded)
        chosenGroup                 ""              (falsy = every group)
        deviceMenuItem              "Active"
        fromDate / toDate           ""              (no time filter = all history)
        serviceTypes                "5,3,6,7,9,10"  (every message type)
        deviceHistories             ""
        filter                      "null"          (FilterType default is null)
        visibleUserIds              "<id>,<id>,..." (whose data to export)
        waypointsNotVisibleSyncIds  ""              (exclude none = all waypoints)
        invisibleRoutesSyncIds      ""              (exclude none = all routes)

``build_export_body`` and ``parse_users`` are pure so the contract stays
unit-testable without a network or a browser.

IMPORTANT operational note: bursts of requests trip Garmin's Cloudflare bot
management, after which even valid cookies are tarpitted (the request hangs).
The tool therefore makes the minimum number of calls - one preflight plus one
POST per requested format - and turns a read timeout into an actionable
"rate-limited" message instead of retrying.
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from .archive import archive_bytes
from .browser_cookies import load_garmin_cookies

_BASE = "https://explore.garmin.com/"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0"

# MESSAGE_TYPES values from map.js: Locate=5, Messages=3, TrackPoints=6,
# Navigation=7, ReferencePoints=9, MapShare=10. The default filter selects all,
# so requesting all of them exports every message type.
_ALL_MESSAGE_TYPE_IDS = (5, 3, 6, 7, 9, 10)

_RATE_LIMITED = (
    "Garmin (Cloudflare) is rate-limiting automated requests right now, so the "
    "export request timed out. Open explore.garmin.com in your browser to confirm "
    "you can sign in, then wait a few minutes and re-run. Avoid repeated retries - "
    "they extend the block."
)
_SESSION_EXPIRED = (
    "Your Garmin Explore session looks signed out or expired. Open "
    "explore.garmin.com/Map in your browser so it loads the signed-in map, then "
    "re-run."
)


def _lookup(row: dict, *names: str) -> Any:
    """Return the first present key from ``names``, matched case-insensitively."""
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def parse_users(payload: Any) -> list[dict]:
    """Normalize a GetUsersSimplified response into ``{id, group_id, checked}`` rows.

    The response is either a bare list of user objects or an envelope such as
    ``{"Users": [...], "Groups": [...]}``; both are handled. Rows without an id are
    dropped. Key casing (``Id``/``GroupID``/``Checked``) is matched loosely because
    it is the one part of the contract not yet confirmed against a live response.
    """
    if isinstance(payload, dict):
        users = _lookup(payload, "Users", "users", "Data", "Result")
        if users is None:
            users = []
    else:
        users = payload
    rows: list[dict] = []
    if not isinstance(users, list):
        return rows
    for user in users:
        if not isinstance(user, dict):
            continue
        raw_id = _lookup(user, "Id", "UserId", "SyncId")
        if raw_id in (None, ""):
            continue
        checked = _lookup(user, "Checked", "IsChecked")
        rows.append(
            {
                "id": str(raw_id),
                "group_id": _lookup(user, "GroupID", "GroupId"),
                "checked": True if checked is None else bool(checked),
            }
        )
    return rows


def build_export_body(users: list[dict], *, chosen_group: str = "") -> dict[str, str]:
    """Build the GetDeviceListForDownload form body that exports everything.

    Every user's id goes into ``visibleUserIds`` (an empty ``chosenGroup`` means
    the page would include all checked users across groups; we include them all so
    a browserless run captures the whole account).
    """
    visible_ids = ",".join(user["id"] for user in users)
    return {
        "chosenGroup": chosen_group,
        "deviceMenuItem": "Active",
        "fromDate": "",
        "toDate": "",
        "serviceTypes": ",".join(str(type_id) for type_id in _ALL_MESSAGE_TYPE_IDS),
        "deviceHistories": "",
        "filter": "null",
        "visibleUserIds": visible_ids,
        "waypointsNotVisibleSyncIds": "",
        "invisibleRoutesSyncIds": "",
    }


def extract_kml_if_kmz(content: bytes) -> bytes:
    """Return inner KML bytes from a KMZ, or pass raw KML through unchanged.

    Garmin delivers a "KML" export as a KMZ - a zip wrapping ``mapdata.kml`` - so
    the raw response starts with the zip magic ``PK`` rather than ``<kml``. GPX
    exports are plain XML and never reach here.
    """
    if content[:4] != b"PK\x03\x04":
        return content
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            kml_names = [name for name in archive.namelist() if name.lower().endswith(".kml")]
            if not kml_names:
                raise RuntimeError("Garmin returned a KMZ with no KML file inside.")
            return archive.read(kml_names[0])
    except zipfile.BadZipFile as error:
        raise RuntimeError("Garmin returned a corrupt KMZ export.") from error


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop urllib from following the SSO redirect so we can detect a dead session."""

    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def _cookiejar(cookies: list[dict]) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    for cookie in cookies:
        domain = cookie["domain"]
        initial_dot = domain.startswith(".")
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=cookie["name"],
                value=cookie["value"],
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=initial_dot,
                domain_initial_dot=initial_dot,
                path=cookie.get("path", "/"),
                path_specified=True,
                secure=bool(cookie.get("secure", True)),
                expires=cookie.get("expires"),
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


def _post(opener, path: str, data: dict, timeout: int) -> tuple[int, str, bytes]:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(_BASE + path, data=body, method="POST")
    request.add_header("User-Agent", _USER_AGENT)
    request.add_header("X-Requested-With", "XMLHttpRequest")
    request.add_header("Referer", _BASE + "Map")
    request.add_header("Accept", "application/json, text/javascript, */*; q=0.01")
    request.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    try:
        response = opener.open(request, timeout=timeout)
        return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()


def _assert_session_live(cookies: list[dict], timeout: int) -> None:
    """Confirm /Map serves the app rather than redirecting to SSO or hanging."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(_cookiejar(cookies)), _NoRedirect()
    )
    request = urllib.request.Request(_BASE + "Map")
    request.add_header("User-Agent", _USER_AGENT)
    request.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    try:
        response = opener.open(request, timeout=timeout)
        status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
    except TimeoutError as error:
        raise RuntimeError(_RATE_LIMITED) from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, (socket.timeout, TimeoutError)):
            raise RuntimeError(_RATE_LIMITED) from error
        raise RuntimeError(f"Could not reach Garmin Explore: {error.reason}") from error
    if status in (301, 302, 303, 307, 308):
        raise RuntimeError(_SESSION_EXPIRED)


def browserless_export(
    data_dir: Path,
    *,
    formats: tuple[str, ...] = ("kml",),
    browser: str | None = None,
    timeout: int = 45,
) -> dict:
    """Export Garmin Explore data over HTTP using the browser's saved session.

    Makes the minimum number of requests: one session check, one user preflight,
    and one POST per requested format. Raises ``RuntimeError`` with actionable
    guidance on an expired session or a Cloudflare rate-limit timeout.
    """
    cookies = load_garmin_cookies(browser)
    _assert_session_live(cookies, timeout)

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookiejar(cookies)))

    status, _content_type, raw = _post(
        opener, "Configuration/User/GetUsersSimplified", {"includeGroups": "true"}, timeout
    )
    if status in (301, 302, 303, 307, 308) or not raw.strip():
        raise RuntimeError(_SESSION_EXPIRED)
    try:
        users = parse_users(json.loads(raw))
    except json.JSONDecodeError as error:
        raise RuntimeError(_SESSION_EXPIRED) from error
    if not users:
        raise RuntimeError(
            "Garmin returned no users for this account, so there is nothing to "
            "export. Confirm the account has an inReach device on explore.garmin.com."
        )

    body = build_export_body(users)
    archive_dir = data_dir / "raw" / "explore"
    results = []
    for format_name in formats:
        file_type = {"kml": "KML", "gpx": "GPX"}[format_name]
        query = urllib.parse.urlencode({"fileType": file_type})
        try:
            status, _content_type, content = _post(
                opener, f"Map/GetDeviceListForDownload?{query}", body, timeout
            )
        except TimeoutError as error:
            raise RuntimeError(_RATE_LIMITED) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise RuntimeError(_RATE_LIMITED) from error
            raise RuntimeError(f"Garmin Explore export failed: {error.reason}") from error
        if status != 200:
            raise RuntimeError(f"Garmin Explore export failed with HTTP {status}.")
        if not content.strip():
            # A 200 with an empty body is Garmin's signature for a stale session.
            raise RuntimeError(_SESSION_EXPIRED)
        if format_name == "kml":
            content = extract_kml_if_kmz(content)
        expected = b"<kml" if format_name == "kml" else b"<gpx"
        if expected not in content[:2000].lower():
            raise RuntimeError(
                "Garmin Explore returned an error instead of an export. A track over "
                "Garmin's export size limit is a common cause."
            )
        path, created = archive_bytes(content, archive_dir, "explore", f".{format_name}")
        results.append({"format": format_name, "path": str(path), "created": created})

    return {"exports": results, "authenticated_via": "saved-cookies", "transport": "http"}
