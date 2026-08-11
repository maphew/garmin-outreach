# Security Policy

## Supported versions

Garmin Outreach is currently an early alpha. Security fixes are applied to the latest version on
the default branch.

## Reporting a vulnerability

Please use GitHub's **Security** tab to submit a private vulnerability report. If private
vulnerability reporting is unavailable, open a minimal issue asking the maintainers to enable a
private reporting channel. Do not include exploit details in a public issue.

You should receive an acknowledgement within seven days. The maintainers will investigate,
coordinate a fix when needed, and credit reporters who want to be identified.

## Protecting private Garmin data

Do not attach any of the following to an issue or vulnerability report:

- Garmin passwords, API credentials, or session cookies;
- browser profiles or files from `data/.browser-profile/`;
- real KML, GPX, GeoPackage, GeoJSON, or Shapefile exports;
- messages, IMEIs, account identifiers, or precise location history.

Create the smallest synthetic reproduction possible. If a browser profile or credential is
exposed, terminate the affected Garmin sessions and rotate relevant credentials immediately.
