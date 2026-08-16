# Garmin Outreach: Maintainer Guide

Last verified: 2026-08-10

This document is the implementation guide for maintainers. Read it before changing acquisition, parsing, identity, cleanup, or output behavior. User-facing setup and commands live in [Readme.md](Readme.md).

## Current status

Version `0.1.0` is a working Python 3.11+ CLI with:

- resumable MapShare KML polling;
- full Explore web-export capture through a persistent Playwright session;
- local GPX/KML ingestion;
- semantic feature classification and deduplication;
- conservative derived-trip generation; and
- GeoPackage, GeoJSON, and Shapefile output.

Current verification covers 11 passing tests, Ruff, a locked uv environment on Python 3.11,
generated-layer reads as `EPSG:4326`, Playwright Chromium launch, and the intended signed-out
headless Explore failure. The authenticated Explore POST has not yet received broad real-account
validation, so the browser integration remains experimental.

## Fast start

```powershell
uv sync --extra browser
uv run --extra browser playwright install chromium

uv run ruff format --check .
uv run ruff check .
uv run pytest
uv pip check
```

CLI smoke test:

```powershell
uv run garmin-outreach --help
uv run garmin-outreach --data-dir data\smoke ingest tests\fixtures\mapshare.kml tests\fixtures\explore.gpx
```

`uv.lock` is committed and is the reproducibility boundary for development and CI. `uv sync`
installs the default `dev` dependency group; add `--extra browser` only when working on Explore
capture. Use `uv add`, `uv add --dev`, and `uv lock` for dependency changes rather than editing a
generated requirements file or installing into `.venv` directly.

Global flags such as `--data-dir` and `--formats` must appear before the subcommand because the CLI uses standard `argparse` subparsers.

## Architecture

```text
MapShare HTTP feed ─┐
Explore browser ────┼─> content-addressed data/raw archive
Local KML/GPX ──────┘                 │
                                      v
                            KML/GPX parsers
                                      │
                                      v
                         Feature intermediate model
                                      │
                           deduplicate by layer + ID
                                      │
                      derive trips without deleting points
                                      │
                                      v
                    GPKG + GeoJSON + Shapefile + summary
```

The central design choice is that acquisition and conversion are separate. Network or browser commands archive source bytes first. `build` can then reproduce all normalized outputs without contacting Garmin.

## Repository map

| Path | Responsibility |
| --- | --- |
| `src/garmin_outreach/cli.py` | `argparse` command surface and environment-variable handling |
| `src/garmin_outreach/archive.py` | Atomic, SHA-256-addressed raw archival |
| `src/garmin_outreach/mapshare.py` | Date-window polling, authentication, retries, resume state, Garmin-host allowlist |
| `src/garmin_outreach/explore.py` | Persistent browser sign-in and capture of Garmin's own export POST |
| `src/garmin_outreach/parsers.py` | Namespace-agnostic KML/GPX parsing and semantic classification |
| `src/garmin_outreach/model.py` | Dependency-light `Feature` model and deterministic deduplication |
| `src/garmin_outreach/cleanup.py` | Derived trip splitting and Haversine distance calculation |
| `src/garmin_outreach/pipeline.py` | Raw-archive rebuild orchestration and parse-error collection |
| `src/garmin_outreach/exporters.py` | GeoPandas/Pyogrio writers, atomic output replacement, Shapefile aliases |
| `tests/fixtures/` | Synthetic, non-private Garmin-like KML and GPX |
| `tests/` | Parser, trip, archive, sync-security, idempotency, and writer tests |

## Non-negotiable invariants

1. Raw data is archival. Never "clean" or rewrite source exports in place.
2. Cleanup may split or annotate derived geometry, but must not delete source track points or source tracks.
3. Repeated syncs and ingests must not multiply equivalent records.
4. Credentials must never be written to configuration, logs, raw archives, output attributes, or command history.
5. A URL receiving Basic-auth credentials must pass the explicit Garmin HTTPS host allowlist.
6. All output geometry is WGS 84 (`EPSG:4326`) and currently two-dimensional. Elevation remains an attribute.
7. Unknown Garmin fields should be preserved in `extra_json` rather than discarded.
8. One corrupt raw batch may be reported in `parse_errors`, but should not prevent other valid history from being rebuilt.
9. Tests and docs must use synthetic or redacted data. Never commit `data/`, browser profiles, real messages, IMEIs, or location history.

## Runtime data layout

```text
data/
├── .writer.lock            # advisory interprocess writer lock; persists between runs; safe to ignore/delete when nothing is running
├── mapshare-state.json
├── .browser-profile/       # equivalent to an authenticated session; sensitive
├── raw/
│   ├── mapshare/*.kml
│   ├── explore/*.{kml,gpx}
│   └── imports/*.{kml,gpx}
└── output/
    ├── garmin-outreach.gpkg
    ├── summary.json
    ├── geojson/*.geojson
    └── shapefile/*.{shp,shx,dbf,prj,cpg}
```

`data/` and `.garmin-outreach/` are ignored. Treat anything already under `data/` as runtime/smoke-test state, not as a checked-in fixture or a source of truth.

Every mutating CLI command (`ingest`, `mapshare`, `explore`, `build`) holds `.writer.lock` for the duration of its work; a concurrent mutating invocation fails fast with exit 2 instead of blocking or interleaving writes. The lock is not reentrant — nested acquisition within the same process also fails fast, with a distinct error message identifying the lock as already held by this process.

## Feature identity and idempotency

`Feature.stable_id()` uses an explicit `feature_id` when available; otherwise it hashes a canonical JSON representation of layer, geometry, and properties. Garmin MapShare events use `garmin:<Id>:<geometry-index>`. GPX track points use their parent line ID and point sequence.

Deduplication keys are `(layer, stable_id)`. If two candidates have the same key, the version with more populated properties wins. Keep the layer in the key: the same upstream entity may legitimately have both point and line representations.

Raw files are addressed by the first 16 hexadecimal characters of SHA-256. `archive_bytes()` checks for an existing digest in the target acquisition directory before writing and uses atomic replacement for new files. Identical bytes obtained through different acquisition types can exist once in each acquisition directory; that provenance boundary is intentional.

MapShare state advances after every successful time window, including an empty window. Later incremental runs overlap the watermark by ten minutes. Existing raw MapShare files are parsed to build the seen-ID set, so a new response is archived only when it contains at least one unseen feature. `--full` safely rescans from 2010.

## MapShare integration

Garmin's documented consumer feed shape is:

```text
https://inreach.garmin.com/feed/share/<MapShareIdentifier>
```

Supported query parameters are `d1`, `d2`, and optional `imei`. Times are sent as UTC `YYYY-MM-DDTHH:MM:SSZ`. Default windows are 31 days. The implementation follows redirects, uses a 120-second timeout, retries `429/5xx` responses up to four attempts, and caps `Retry-After` sleeps at 30 seconds.

Authentication is optional HTTP Basic authentication. Password input comes only from `GARMIN_MAPSHARE_PASSWORD` or an interactive `--ask-password` prompt. The username comes from `GARMIN_MAPSHARE_USERNAME` and defaults to an empty string, which is the common password-only MapShare case.

Accepted feed hosts are deliberately narrow:

- `inreach.garmin.com`
- `share.garmin.com`
- `explore.garmin.com`
- subdomains ending in `.inreach.garmin.com`
- regional hosts ending in `-share.explore.garmin.com`

Do not broaden this list casually. It protects Basic-auth secrets from attacker-controlled URLs.

Garmin publishes a 2,000-request/hour limit. The current implementation stays far below this in ordinary use but does not implement a global rate ledger across processes.

## Explore browser export

There is no documented consumer account-history API. The web application currently constructs this request:

```text
POST <culture-relative>/Map/GetDeviceListForDownload?fileType=KML|GPX
```

Observed POST fields:

- `chosenGroup`
- `deviceMenuItem`
- `fromDate`
- `toDate`
- `serviceTypes`
- `deviceHistories`
- `filter`
- `visibleUserIds`
- `waypointsNotVisibleSyncIds`
- `invisibleRoutesSyncIds`

Do not hard-code account-specific values. `CAPTURE_EXPORT_REQUEST` finds Garmin's Knockout view model from `#mapfilters` or other bound elements, calls `clearFilters()`, temporarily replaces `jQuery.fileDownload`, invokes the page's own `exportData()` method, and captures the URL/body Garmin generated. Python then posts that body using the same Playwright browser context, which shares the authenticated cookies. Hidden waypoint/route exclusion lists are blanked so all account-available items are requested.

The first headed run lets the user complete Garmin SSO and 2FA normally. The persistent profile defaults to `data/.browser-profile`. Later `--headless` runs reuse it. The tool never asks for, reads, or stores the Garmin account password.

Likely failure modes:

- Garmin renames `#mapfilters`, the Knockout model, `exportData`, or `jQuery.fileDownload`.
- Garmin changes the export endpoint or POST fields.
- Authentication expires; headless mode should instruct the user to repeat one headed run.
- The website returns an HTML error, often because a source track exceeds Garmin's export limit.
- Localization changes behavior despite the explicit `/en-US/Map` entry URL.

When fixing web capture, derive state from the live page again. Avoid replacing this with a private endpoint call containing guessed IDs.

## Parsing contract

The intermediate `Feature` supports `Point` and `LineString`. Coordinates are always `(longitude, latitude)`.

KML handling:

- Ignores namespace prefixes by using local tag names.
- Recurses through `Document` and `Folder` containers and retains folder paths.
- Parses `Point`, `LineString`, and `gx:Track`.
- Parses both `Data/value` and `SimpleData` extended data.
- Converts Garmin units embedded in strings into numeric elevation metres, velocity km/h, and course degrees.
- Normalizes ISO timestamps and Garmin's US-formatted `Time UTC` to UTC `Z` strings.

MapShare point classification:

| Signal | Layer |
| --- | --- |
| `Tracking message` event or track-point folder | `track_points` |
| text/quick/check-in/shared-map/test message event | `messages` |
| waypoint/navigation or reference-point event | `waypoints` |
| other event-bearing point | `events` |
| ordinary point without an event | `waypoints` |

Line classification uses folder/name context: `course` → `courses`, `route` → `routes`, otherwise `tracks`.

GPX handling:

- top-level `wpt` → point layer, usually `waypoints`;
- each `rte` → one `routes` line;
- each `trkseg` with at least two points → one `tracks` line; and
- every `trkpt` → one `track_points` observation linked to its parent segment.

Garmin sometimes flattens routes into tracks before delivery. Do not guess a lost distinction unless a folder name, GPX element, or explicit extension supports it.

## Trip derivation

`split_trips()` groups timestamped track points by, in order:

1. `parent_id` (GPX track segment);
2. `imei`;
3. `device_name`;
4. `source_file`; or
5. `unknown`.

Points are time-sorted. A new segment begins after a time gap over six hours or a jump of at least 10 km implying more than 200 km/h. Every source point gets a derived `trip_id`. A `trips` line is written only for segments with at least two points. Distances use a spherical Haversine calculation.

These thresholds are CLI-configurable. Future cleanup should remain explainable and reversible. Prefer adding quality flags or derived layers over silently deleting observations.

## GIS writers

GeoPandas constructs each layer and Pyogrio writes it. GeoPackage creation goes to a temporary file and then atomically replaces the destination. GeoJSON and Shapefile directories are built in temporary sibling directories and swapped into place.

Shapefile fields are limited to ten characters. Stable aliases live in `SHAPEFILE_ALIASES`; `shapefile/fields.json` records every original-to-short mapping. Add explicit aliases for new common fields to avoid unstable numeric suffixes.

Only non-empty layers are created. `summary.json` includes feature counts, layer counts, formats, paths, raw input count, parse errors, and a per-layer `bbox` (omitted for a layer whose bounds are non-finite).

## Verification expectations

For parser changes:

1. Add the smallest synthetic or redacted fixture reproducing the schema.
2. Assert geometry, layer, normalized units/timestamps, and stable identity.
3. Run an ingest/rebuild twice and verify counts do not grow.

For writer changes, read the generated GeoPackage back with GeoPandas and assert the layer CRS and at least one important property. File existence alone is insufficient.

For Explore changes, test both states:

- a fresh `--headless` profile yields the intentional sign-in message; and
- a user-authenticated headed profile returns valid KML/GPX and archives it once.

Never add real account exports to the test suite.

## Known limitations and prioritized next work

1. Live-validate the authenticated Explore export and add a redacted structural fixture. This is the highest-value next step.
2. Investigate Garmin's over-500-point track failure. Date chunking may help event history but may not split a stored library track; do not claim a workaround without testing.
3. Add KMZ ingestion if real exports require it. Use safe ZIP handling and reject path traversal or decompression bombs.
4. Use tracking-on/off events as optional trip boundaries while preserving the current conservative fallbacks.
5. Add an explicit normalized schema version before downstream users depend on field names.
6. Add larger streaming/parser benchmarks if personal histories make full rebuilds slow. The current implementation loads features in memory and reparses MapShare raw files to recover seen IDs.
7. Consider a SQLite event index only when measured history size justifies the complexity.

Garmin-controlled limitations that cannot be fixed locally:

- MapShare only exposes event types enabled in MapShare settings.
- Ordinary feeds omit SOS message content.
- Explore website exports omit activities.
- Garmin exports routes as tracks in some paths.
- The website may reject tracks over 500 points.

Activities shared as GPX from the mobile app can still be ingested.

## Research record

Primary references checked on 2026-08-10:

- [Garmin: About inReach KML Feeds](https://support.garmin.com/en-US/?faq=tdlDCyo1fJ5UxjUbA9rMY8) — URL shapes, date parameters, fields, event types, Basic auth, visibility, SOS omission, and rate limits.
- [Garmin: Exporting Data from the Explore Website](https://support.garmin.com/en-US/?faq=EQgCLV89re2liuSa5xPoG6) — supported web workflow and activity/route/track limitations.
- [Garmin: Exporting GPX Files from the Explore App](https://support.garmin.com/en-US/?faq=uw7aE4p5MB2GRG2B6xxQCA) — activity and collection export fallback.
- [Garmin inReach Portal Connect](https://developer.garmin.com/inreach-portal/download/) — documented API is aimed at professional/enterprise integrations, not ordinary consumer history export.

Existing open-source work checked included [BHSPitMonkey/homeassistant-garmin-mapshare](https://github.com/BHSPitMonkey/homeassistant-garmin-mapshare), which is useful corroboration for current MapShare field shapes and password-only Basic auth but targets live Home Assistant state rather than historical GIS archival.

The Explore request shape was recovered from Garmin's then-current public `map.js` bundle rather than copied from a third-party implementation. Recheck it whenever the browser capture breaks.

## Before handing off again

- Run Ruff, Pytest, compileall, and `uv pip check` through the uv-managed environment.
- Update the test count and last-verified date near the top of this file.
- Record any Garmin bundle/endpoint changes here without including account IDs or tokens.
- Keep user-facing commands and limitations synchronized with `Readme.md`.
- Summarize any schema change and whether existing outputs must be rebuilt.
