from garmin_outreach.browser_cookies import (
    _expires_seconds,
    _normalize_same_site,
    to_playwright_cookies,
)


def _raw(**overrides):
    base = {
        "name": "GARMIN-SSO",
        "value": "token",
        "domain": ".garmin.com",
        "path": "/",
        "secure": True,
        "http_only": True,
        "expires": 1893456000,
        "same_site": "lax",
    }
    base.update(overrides)
    return base


def test_converts_and_renames_playwright_fields():
    (cookie,) = to_playwright_cookies([_raw()])
    assert cookie == {
        "name": "GARMIN-SSO",
        "value": "token",
        "domain": ".garmin.com",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "expires": 1893456000,
        "sameSite": "Lax",
    }


def test_keeps_only_garmin_domains():
    raw = [
        _raw(domain=".garmin.com"),
        _raw(domain="explore.garmin.com"),
        _raw(domain="us0.explore.garmin.com"),
        _raw(domain=".google.com"),
        _raw(domain="notgarmin.com"),
        _raw(domain="evil-garmin.com.attacker.net"),
    ]
    domains = {c["domain"] for c in to_playwright_cookies(raw)}
    assert domains == {".garmin.com", "explore.garmin.com", "us0.explore.garmin.com"}


def test_skips_rows_missing_name_or_value():
    raw = [_raw(name=""), _raw(value=None), _raw()]
    assert len(to_playwright_cookies(raw)) == 1


def test_omits_expires_when_session_cookie():
    (cookie,) = to_playwright_cookies([_raw(expires=0)])
    assert "expires" not in cookie


def test_millisecond_expiry_is_scaled_to_seconds():
    # rookiepy reports Firefox expiries in milliseconds; Playwright wants seconds.
    (cookie,) = to_playwright_cookies([_raw(expires=1817932207675)])
    assert cookie["expires"] == 1817932207


def test_expires_seconds_helper():
    assert _expires_seconds(1893456000) == 1893456000  # already seconds
    assert _expires_seconds(1817932207675) == 1817932207  # milliseconds
    assert _expires_seconds(0) is None
    assert _expires_seconds(-1) is None
    assert _expires_seconds(None) is None


def test_same_site_normalization():
    assert _normalize_same_site("no_restriction") == "None"
    assert _normalize_same_site(2) == "Strict"
    assert _normalize_same_site("unspecified") is None
    assert _normalize_same_site(None) is None


def test_same_site_absent_when_unspecified():
    (cookie,) = to_playwright_cookies([_raw(same_site="unspecified")])
    assert "sameSite" not in cookie


def test_accepts_playwright_style_alias_keys():
    (cookie,) = to_playwright_cookies(
        [{"name": "n", "value": "v", "domain": "garmin.com", "httpOnly": True}]
    )
    assert cookie["httpOnly"] is True
    assert cookie["domain"] == "garmin.com"
