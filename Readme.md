# Garmin Outreach

Garmin Outreach turns Garmin inReach and Explore history into useful, separate GIS layers. It preserves every raw response or export, deduplicates repeated records, and rebuilds GeoPackage, GeoJSON, and Shapefile outputs deterministically.

> [!IMPORTANT]
> Garmin Outreach is an independent, unofficial open-source project. It is not affiliated with,
> endorsed by, or supported by Garmin. Garmin, inReach, MapShare, and Garmin Explore are trademarks
> of Garmin Ltd. or its subsidiaries. Use the acquisition features only with accounts and data you
> are authorized to access, and review Garmin's applicable terms before use.

The project supports three acquisition paths:

1. Incremental, unattended polling of Garmin's documented MapShare KML feed.
2. Full consumer Explore KML/GPX capture through a persistent signed-in browser session.
3. Ingestion of any GPX/KML file exported by the Explore website or mobile app.

All three feed the same normalization pipeline.

## What it produces

The default output directory is `data/output/`:

| Output | Contents |
| --- | --- |
| `garmin-outreach.gpkg` | One QGIS-ready GeoPackage with a layer per data type |
| `geojson/*.geojson` | Portable GeoJSON files |
| `shapefile/*.shp` | Shapefiles plus `fields.json`, which records shortened field names |
| `summary.json` | Feature counts, input counts, parse errors, and written paths |

Layers are created when that kind of data exists:

- `messages`: text, quick/check-in, MapShare, and test messages
- `track_points`: timestamped tracking observations
- `waypoints`: waypoints, reference points, and ordinary KML/GPX points
- `events`: tracking state, location requests, emergency metadata made available by Garmin, and other inReach events
- `tracks`, `routes`, `courses`: source linework, kept separate whenever the source format retains the distinction
- `trips`: conservative linework derived from ordered track points

All layers use WGS 84 (`EPSG:4326`). Original input files remain under `data/raw/`; derived cleanup never deletes or rewrites the source observations.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and sync the project. uv
installs a suitable Python 3.11 interpreter when needed, creates the project environment, and
installs the locked dependencies and Garmin Outreach itself:

```powershell
winget install --id=astral-sh.uv -e
uv sync
```

No environment activation is needed. Run project commands through `uv run`; uv keeps the
environment in sync with `pyproject.toml` and `uv.lock` automatically:

```powershell
uv run garmin-outreach --help
uv run pytest
```

## Incremental MapShare sync

Garmin documents a Raw KML Feed with UTC `d1`/`d2` query parameters and optional HTTP Basic authentication. Find the link in Garmin Explore under **MapShare → Feeds → Raw KML Feed**.

```powershell
$env:GARMIN_MAPSHARE_ID = "your-mapshare-identifier"
uv run garmin-outreach mapshare
```

For a password-protected MapShare page, keep the password out of shell history:

```powershell
uv run garmin-outreach mapshare your-mapshare-identifier --ask-password
```

The first run scans from 2010 in 31-day windows. Later runs resume from `data/mapshare-state.json` with a small overlap. A response is archived only when it contains a previously unseen feature, and normalized features are deduplicated by Garmin event ID. Use `--full` after changing MapShare visibility settings to rescan history safely.

Useful options:

```powershell
uv run garmin-outreach mapshare your-id --start 2024-01-01 --end 2025-01-01
uv run garmin-outreach mapshare https://inreach.garmin.com/feed/share/your-id --imei 300000000000000
uv run garmin-outreach --formats gpkg,geojson mapshare your-id
```

The sync follows Garmin's published polling limit and retries transient failures. A supplied feed URL must be HTTPS and hosted by Garmin so credentials cannot be sent to an arbitrary host.

## Capture the full Explore web export (experimental)

The consumer Explore site does not provide a documented account-history API. Garmin's own web application does have a KML/GPX export request. This command opens a persistent Chromium profile, waits for normal Garmin sign-in, obtains the current export parameters from the loaded map, clears date/message filters, requests all visible-account data, archives the result, and rebuilds the GIS outputs.

This integration is experimental because it depends on Garmin's current website implementation. It has been verified through browser launch and signed-out failure handling, but the authenticated export flow still needs broader real-account validation. Prefer a manual Explore export followed by `ingest` when reliability is more important than unattended capture.

Install the optional browser support once:

```powershell
uv sync --extra browser
uv run --extra browser playwright install chromium
```

First run (complete Garmin sign-in in the opened browser):

```powershell
uv run --extra browser garmin-outreach explore --export-formats both
```

Later runs can be unattended with the saved local session:

```powershell
uv run --extra browser garmin-outreach explore --headless
```

The browser profile is stored under `data/.browser-profile/` and is ignored by Git. Treat it like a password. No Garmin password is accepted or stored by Garmin Outreach.

This web capture intentionally derives the request from Garmin's currently loaded page. If Garmin changes the site, it fails with a clear error instead of guessing at a new private endpoint.

## Ingest existing exports

Any number of KML and GPX files can be imported at once:

```powershell
uv run garmin-outreach ingest "$HOME\Downloads\explore.kml" trip.gpx
```

Inputs are copied into the content-addressed raw archive. Re-ingesting the same bytes creates nothing new. Rebuild at any time with:

```powershell
uv run garmin-outreach build
```

## Trip cleanup

The `trips` layer joins consecutive track points for the same device or source segment. It splits, but never deletes, when:

- the time gap exceeds 6 hours; or
- a jump is at least 10 km and implies a speed over 200 km/h.

These deliberately conservative defaults avoid drawing spurious lines between separate outings. Adjust them per run:

```powershell
uv run garmin-outreach build --trip-gap-hours 12 --jump-km 25 --max-speed-kmh 160
```

## Automation example

Once an initial sync or browser sign-in has succeeded, Windows Task Scheduler can invoke uv
without activating an environment:

```text
Program/script: C:\path\to\uv.exe
Arguments:      run garmin-outreach mapshare your-id
Start in:       C:\path\to\garmin-outreach
```

For unattended Explore capture, use `run --extra browser garmin-outreach explore --headless` as
the arguments.
Use `where.exe uv` to find the executable. Keep the task's working directory set to the repository,
or pass an absolute `--data-dir` before the subcommand.

## Garmin limitations

The boundaries here come from Garmin, not the converter:

- The [MapShare KML feed](https://support.garmin.com/en-US/?faq=tdlDCyo1fJ5UxjUbA9rMY8) includes only event/message types enabled in the account's MapShare settings. Garmin documents its date parameters, fields, event types, Basic authentication, and rate limit.
- Garmin's [Explore export documentation](https://support.garmin.com/en-US/?faq=EQgCLV89re2liuSa5xPoG6) says activities are not exported by the website, routes are exported as tracks, and tracks over 500 points can fail. Activities can be shared as GPX from the Explore mobile app and then ingested here.
- SOS messages are omitted from ordinary MapShare feeds by Garmin except for specially arranged emergency-response accounts.
- Once Garmin has flattened a route into a track, no parser can recover that lost distinction reliably. Garmin Outreach preserves distinctions present in folder names and GPX element types and does not fabricate the rest.

The closest existing open-source work found was aimed at live-location consumers, such as the [Home Assistant Garmin MapShare integration](https://github.com/BHSPitMonkey/homeassistant-garmin-mapshare). Garmin Outreach adds historical windowing, raw archival, multi-format ingestion, semantic GIS layers, reproducible outputs, and trip cleanup.

## Privacy

Location history and message text are sensitive. `data/` is excluded by `.gitignore`; keep it that way. Do not commit raw exports, browser profiles, MapShare passwords, or generated GIS files to a public repository.

Browser profiles are equivalent to authenticated sessions. If one is exposed, sign out of Garmin sessions and rotate any affected credentials. Please report security vulnerabilities using the process in [SECURITY.md](SECURITY.md), without attaching private location history or account exports.

Implementation and maintainer notes are available in [Tech_dev.md](Tech_dev.md).
