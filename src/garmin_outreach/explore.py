from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urljoin

from .archive import archive_bytes
from .browser_cookies import load_garmin_cookies

CAPTURE_EXPORT_REQUEST = r"""
(formatNumber) => {
  const element = document.querySelector('#mapfilters');
  if (!element || !window.ko || !window.jQuery) {
    throw new Error('The Garmin map filters did not finish loading.');
  }
  const boundElements = [element, ...element.querySelectorAll('[data-bind]')];
  let model = boundElements.map((candidate) => window.ko.dataFor(candidate))
    .find((candidate) => candidate && typeof candidate.exportData === 'function');
  if (!model) {
    model = [...document.querySelectorAll('[data-bind]')]
      .map((candidate) => window.ko.dataFor(candidate))
      .find((candidate) => candidate && typeof candidate.exportData === 'function');
  }
  if (!model || typeof model.exportData !== 'function') {
    throw new Error('Garmin changed the map export model.');
  }
  if (typeof model.clearFilters === 'function') model.clearFilters();
  return new Promise((resolve, reject) => {
    const original = window.jQuery.fileDownload;
    const timeout = setTimeout(() => {
      window.jQuery.fileDownload = original;
      reject(new Error('Timed out while preparing the Garmin export.'));
    }, 15000);
    window.jQuery.fileDownload = (url, options) => {
      clearTimeout(timeout);
      window.jQuery.fileDownload = original;
      resolve({url, data: options.data});
      return {abort() {}};
    };
    try {
      model.exportData(formatNumber);
    } catch (error) {
      clearTimeout(timeout);
      window.jQuery.fileDownload = original;
      reject(error);
    }
  });
}
"""


def capture_explore(
    data_dir: Path,
    *,
    formats: tuple[str, ...] = ("kml",),
    profile_dir: Path | None = None,
    headless: bool | None = None,
    login_timeout_seconds: int = 600,
    browser: str | None = None,
    use_saved_cookies: bool = True,
) -> dict:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Browser capture is optional. Install it with `pip install -e .[browser]`, "
            "then run `playwright install chromium`."
        ) from error

    # Borrow a live session from the user's real browser. When one exists there is
    # no sign-in screen and no Cloudflare challenge, so we can run headless; only
    # when it is missing do we fall back to a visible interactive login.
    cookies: list[dict] = []
    cookie_error: str | None = None
    if use_saved_cookies:
        try:
            cookies = load_garmin_cookies(browser)
        except RuntimeError as error:
            cookie_error = str(error)
    if headless is None:
        headless = bool(cookies)

    profile = profile_dir or data_dir / ".browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    archive_dir = data_dir / "raw" / "explore"
    results = []
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=headless, accept_downloads=True
            )
            if cookies:
                context.add_cookies(cookies)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(
                    "https://explore.garmin.com/en-US/Map",
                    wait_until="domcontentloaded",
                    timeout=120_000,
                )
                try:
                    page.locator("#mapfilters").wait_for(state="attached", timeout=8_000)
                except PlaywrightTimeout:
                    if headless:
                        detail = cookie_error or (
                            "The saved browser session is signed out or expired."
                        )
                        raise RuntimeError(
                            f"{detail} Sign in to explore.garmin.com in your browser and "
                            "re-run, or run with --no-headless to sign in interactively."
                        ) from None
                    print(
                        "Complete Garmin sign-in in the opened browser; export will "
                        "continue automatically.",
                        file=sys.stderr,
                    )
                    page.locator("#mapfilters").wait_for(
                        state="attached", timeout=login_timeout_seconds * 1000
                    )
                page.wait_for_timeout(1500)
                for format_name in formats:
                    number = {"kml": 0, "gpx": 1}[format_name]
                    captured = page.evaluate(CAPTURE_EXPORT_REQUEST, number)
                    body = {
                        key: "" if value is None else str(value)
                        for key, value in captured["data"].items()
                    }
                    # Garmin's export honors hidden-library state. Blank these fields so the
                    # capture requests every waypoint and route the account makes available.
                    body["waypointsNotVisibleSyncIds"] = ""
                    body["invisibleRoutesSyncIds"] = ""
                    url = urljoin(page.url, captured["url"])
                    response = context.request.post(url, form=body, headers={"Referer": page.url})
                    if not response.ok:
                        raise RuntimeError(
                            f"Garmin Explore export failed with HTTP {response.status}"
                        )
                    content = response.body()
                    expected = b"<kml" if format_name == "kml" else b"<gpx"
                    if expected not in content[:2000].lower():
                        raise RuntimeError(
                            "Garmin Explore returned an error instead of an export. "
                            "A track over Garmin's export limit is a common cause."
                        )
                    path, created = archive_bytes(
                        content, archive_dir, "explore", f".{format_name}"
                    )
                    results.append({"format": format_name, "path": str(path), "created": created})
            finally:
                context.close()
    except RuntimeError:
        raise
    except PlaywrightError as error:
        message = str(error)
        if "Executable doesn't exist" in message:
            message = "Chromium is not installed; run `playwright install chromium`."
        raise RuntimeError(f"Browser capture failed: {message}") from error
    return {
        "exports": results,
        "profile_dir": str(profile),
        "authenticated_via": "saved-cookies" if cookies else "interactive-login",
    }
