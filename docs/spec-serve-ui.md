# Spec: `garmin-outreach serve` — local web UI

Status: v2 — revised after cross-agent review (see Review record below)
Author: drafted by Claude (Fable 5) for maphew, 2026-08-15
Related: mobile map viewing is out of scope (handled by QField reading `garmin-outreach.gpkg`).

## Review record

Draft v1 was reviewed independently by two model families on 2026-08-15:

- `docs/spec-serve-ui-review-claude.md` — Claude reviewer agent (opus tier)
- `docs/spec-serve-ui-review-codex.md` — Codex CLI reviewer (GPT family)

The reviews converged on the major findings (CLI dispatch bug, stored-XSS/Jinja2,
CSRF + DNS rebinding, offline basemap gap, non-atomic output snapshots, Windows
file locking, SSE contract). This v2 incorporates them. Divergences resolved:

- **Starlette over FastAPI** (Claude A10): nothing here needs pydantic/OpenAPI/DI;
  Codex called FastAPI "reasonable" but did not require it. Adopted Starlette.
- **datastar-py pin**: Codex verified current stable is 1.0.2 (2026-06); adopted
  `>=1.0.2,<2` with the vendored browser asset at exactly the locked version and
  a protocol-compatibility test (satisfies Claude's no-drift requirement).
- **CSP `unsafe-eval`**: Claude says Datastar expression evaluation needs it;
  Codex says don't loosen for MapLibre (use the CSP bundle). Resolution: use
  `maplibre-gl-csp.js` + worker so MapLibre never needs eval; determine
  empirically against pinned Datastar 1.0.2 whether `script-src` needs
  `'unsafe-eval'`, grant the minimum that makes the pinned version work, and
  lock the final header string with a test.

## 1. Purpose

Add a `serve` subcommand that runs a small local web server presenting a
dashboard, messages timeline, and map view over the existing `data/` outputs,
and (later phases) buttons to trigger sync/build runs with live progress.

Design intent: self-contained and lightweight. No node toolchain, no build
step, no cloud. Server-rendered HTML driven by Datastar (hypermedia + SSE),
with a single imperative-JS island for the MapLibre map.

## 2. Goals

- One command (`uv run --extra ui garmin-outreach serve`) starts the UI on
  loopback.
- Read-only visibility first: freshness, feature counts, sanitized parse
  errors, messages timeline, interactive map of existing GeoJSON layers.
- Later: trigger `build`, `mapshare`, and `explore --transport http` from the
  UI with streamed progress.
- Fully offline: all JS/CSS assets vendored; zero external browser requests.
  **Consequence stated plainly: the v1 map has no basemap** — data renders on a
  plain background (an inline local style with background + overlay layers, plus
  fit-to-data and scale controls). Roads/terrain/labels are absent by design; an
  online basemap, if ever added, is a separate explicit opt-in (`--tile-url`)
  with its own CSP relaxation and privacy warning.
- Preserve existing invariants: raw archive append-only, deterministic rebuilds,
  no credentials stored or accepted via the browser, `data/` never committed.

## 3. Non-goals

- No mobile app (QField covers mobile viewing).
- No authentication or remote-access mode in v1 (see §8: non-loopback binds are
  refused, not warned about).
- No editing of features, layers, or the raw archive from the UI.
- No routes into `data/raw/`, `data/.browser-profile/`, or arbitrary paths.
- No Playwright (`--transport browser`) flow from the UI; interactive first
  sign-in stays on the CLI.
- No bundled basemap tiles/glyphs/sprites.

## 4. Dependencies and packaging

New optional extra:

```toml
[project.optional-dependencies]
ui = [
  "starlette>=0.40,<1",
  "uvicorn>=0.30,<1",
  "jinja2>=3.1,<4",
  "datastar-py>=1.0.2,<2",
]
```

Notes:

- Starlette, not FastAPI: no request models, OpenAPI, or DI are used; avoids the
  pydantic-core compiled dependency subtree.
- Jinja2 with `autoescape=True` and `StrictUndefined` is a security control, not
  a convenience (see §8).
- Adding the extra requires regenerating `uv.lock` (`uv lock`); CI runs
  `uv sync --locked`.
- Test strategy for CI (which installs no extras today): add the `ui` packages
  to the `dev` dependency group so `uv run pytest` collects `tests/test_serve*`
  everywhere, and guard with `pytest.importorskip("starlette")` so a bare
  environment degrades to skip rather than a collection error.

Vendored static assets under `src/garmin_outreach/serve/static/`, loaded via
`importlib.resources` (never source-checkout-relative paths):

- `datastar.js` at exactly the version matching the locked `datastar-py`
  (currently 1.0.2). A test asserts the two agree so they cannot drift.
- `maplibre-gl-csp.js` + `maplibre-gl-csp-worker.js` + `maplibre-gl.css` (the
  CSP bundle, with the worker URL set explicitly). MapLibre is ~1 MB minified;
  this is a deliberate, recorded cost.
- `VENDORED.md` records, per asset: exact version, source URL, SHA-256,
  license, retrieval date, and the update procedure. Upstream license files
  (MapLibre BSD-3-Clause, Datastar MIT) are vendored alongside and included in
  wheels/sdists.
- Static filenames are content-hashed (hash in the filename, not a query
  string); only these get long immutable cache headers.

Core install unchanged. The `serve` command imports the UI runner lazily inside
its branch and, on `ImportError`, raises `RuntimeError` naming both install
forms (`uv sync --extra ui` and `pip install -e .[ui]`), matching the `browser`
extra's error pattern (exit 2 via the existing CLI handler).

## 5. CLI surface and startup

```
garmin-outreach serve [--host 127.0.0.1] [--port 8477] [--open/--no-open]
```

- v1 accepts only loopback hosts (`127.0.0.1`, `localhost`, `::1`). Any other
  value, including `0.0.0.0`/`::`/empty, exits with an error explaining that
  remote access is out of scope and pointing at Tailscale/WireGuard + a future
  authenticated mode. No warn-and-continue path exists.
- `--open` (default on) opens the browser only after the socket has bound
  successfully (uvicorn startup hook), never before; app-creation failure must
  not open a tab.
- Port in use → clear one-line error naming the port and `--port`, not a
  traceback. Port 8477 is a default preference; tests use port 0.
- Honors global `--data-dir`; the path is `resolve()`d once at startup and all
  later joins use the resolved base.
- **CLI dispatch prerequisite**: `main()`'s current fall-through
  (`cli.py:165`, `getattr(args, "no_build", False)`) would run `rebuild()` with
  missing attributes after `serve` exits. Dispatch is refactored to explicit
  per-command handling before `serve` is added; only `build` and
  ingest/mapshare/explore-without-`--no-build` may rebuild. Regression test
  required.
- Stdout contract: every other command prints one JSON result. `serve` keeps
  stdout clean (human logs to stderr) and exits 0 on clean shutdown.
- Runs exactly one uvicorn worker, `reload=False` — in-process locks and the
  event bus assume a single process.

## 6. Architecture

```
src/garmin_outreach/serve/
  __init__.py     # create_app(data_dir) -> Starlette; run(host, port, ...)
  artifacts.py    # artifact adapter: reads, validates, and shapes all data/ IO
  views.py        # route handlers rendering Jinja2 templates
  templates/      # Jinja2 templates (autoescape, StrictUndefined)
  jobs.py         # later phase: job runner + SSE event bus
  static/         # vendored assets + app.css + map.js
```

Principles:

- The server is a presentation layer over pipeline artifacts; it never parses
  KML/GPX. All filesystem access goes through `artifacts.py`.
- **Outputs are not one atomic snapshot.** Individual files are `os.replace`d,
  but `_replace_directory()` (`exporters.py:131-146`) leaves a window where
  `geojson/` does not exist, and `summary.json` is written twice per rebuild
  with different shapes (`exporters.py:107` then `pipeline.py:49-54`). The
  adapter therefore: reads files fully into memory before responding (never
  streams an open handle — a held handle also breaks the exporter's directory
  rename on Windows); retries once on `FileNotFoundError`/`PermissionError`;
  treats every summary field as optional (`.get()` with defaults); and returns
  friendly empty/degraded states (or 503 during a live rebuild), never a 500.
- Capability model: `--formats` may exclude geojson entirely, and only
  non-empty layers are written. The adapter reports capabilities
  (`geojson_available`, per-layer presence) and views render the exact CLI
  command to fix an absent capability instead of empty panels.
- The canonical layer registry is defined once (hardcoded closed set:
  `track_points, messages, waypoints, events, tracks, routes, courses, trips`)
  and shared by the router allowlist and the views — never derived from
  directory listings or summary content.
- Datastar drives all dynamic UI via SSE patches. The map is an island:
  `map.js` initializes MapLibre once, is idempotent across Datastar DOM morphs
  (its root node is preserved), and communicates via a small custom-event
  contract (layer toggle, focus-feature).

## 7. Routes

### Phase A (read-only)

| Route | Returns |
| --- | --- |
| `GET /` | Dashboard: freshness, layer counts, sanitized parse-error summaries, capabilities |
| `GET /messages` | Messages timeline (paged) |
| `GET /map` | Map page (MapLibre island + layer toggles) |
| `GET /api/layers/{name}.geojson` | Layer content via the adapter; `name` must be in the layer registry and contain no `/` after percent-decoding; 404 unknown, friendly absence state |
| `GET /api/summary` | **Shaped** summary (see below), never the raw file |
| `GET /static/*` | Vendored assets, content-hashed, immutable cache |

Shaped summary response: per-layer counts, `input_files`, parse-error count
plus sanitized entries (basename + error category only — raw paths and
exception text stay in the terminal log), capabilities, and freshness state.
No absolute filesystem paths, no `feed_url`, no MapShare identifier.

Freshness states (rendered with the exact command to copy):

- `outputs_missing` — no readable summary.
- `outputs_stale` — some raw file mtime is newer than the published summary
  (raw scan cached for a few seconds; append-only archive makes mtime a fair
  hint). Hint only — `serve` never auto-builds (empty archive would crash
  `rebuild()`, and startup must stay read-only and fast).
- `mapshare_last_window` — `last_success_utc` from `mapshare-state.json`,
  labeled as MapShare polling state specifically (it says nothing about
  Explore or ingest).
- `freshness_unknown` — malformed state/summary.

Messages timeline: fields are `text`, `timestamp_utc`, `event` (per
`parsers.py`; there is no `type` on KML messages) — all optional, missing
values rendered as explicit placeholders. Ordering: `timestamp_utc` descending,
stable tiebreak on the feature's stable id, undated messages in a labeled
bucket at the end. Server-side parse-once cache keyed on the file's
`(mtime_ns, size)`. Paging via page-number query param only (no free-text in
URLs; search, if added later, uses POST bodies — URLs land in access logs and
browser history).

Map: local inline style (background + overlays), fit-to-data from per-layer
`bbox` values added to `summary.json` by `exporters.py` (small pipeline change,
benefits CLI consumers too). Per-layer property allowlist applied by the
adapter before serving — `source_file`, `imei`, `extra_json`, and similar
internals are stripped from the HTTP surface. Popups are built with DOM nodes
and `textContent`, never HTML interpolation.

### Phase B (jobs + SSE)

| Route | Behavior |
| --- | --- |
| `POST /api/jobs/build` | Rebuild, with bounded trip-cleanup params |
| `POST /api/jobs/mapshare` | MapShare sync **then rebuild** on success |
| `POST /api/jobs/explore` | Browserless Explore export **then rebuild** on success |
| `GET /api/events` | SSE: typed job progress + dashboard refresh signals |

Job semantics:

- **Shared orchestration services** (`run_build`, `run_mapshare`,
  `run_explore_http`) used by both CLI and UI so behavior cannot drift; the UI
  never calls the bare acquisition functions (acquisition without rebuild would
  leave the displayed outputs stale). Acquisition success + build failure is
  reported as `partial_success`; archived raw bytes are always preserved.
- **Single-flight** in-process lock; concurrent POST → 409 with the running
  job's snapshot. POST accepted → 202 with the job snapshot. Job states:
  `running`, `succeeded`, `partial_success`, `failed` (state survives for the
  server process lifetime only).
- **Interprocess writer exclusion**: an advisory lock file under `data/`
  honored by both the CLI mutating commands and the job runner, so a terminal
  `garmin-outreach mapshare` and a UI job cannot interleave. Separate work item;
  until it lands the spec and UI state plainly that concurrent CLI use is
  unguarded.
- Inputs: job POSTs accept only bounded numeric cleanup params and enum format
  choices. Never a feed URL, identifier, password, or cookie material from the
  browser. MapShare identifier comes from `GARMIN_MAPSHARE_ID` (env only — there
  is no config system) or the host-allowlist-revalidated `feed_url` already in
  `mapshare-state.json`; if neither exists the button is disabled with the env
  var named. Password: `GARMIN_MAPSHARE_PASSWORD` env only.
- **Explore caution** (project memory: Cloudflare tarpits bursts): manual
  trigger only, never auto-run/retried/polled; one attempt per click (which
  still performs the existing session check + one export POST per format).
  Requires the `browser` extra's cookie reader — detected as a capability;
  absent → button disabled naming both extras.
- Progress is a **typed event** (`stage`, window/counts, pre-sanitized display
  string) — never raw strings that could carry `feed_url`, identifiers, paths,
  or message text. `sync_mapshare` gains an optional progress callback invoked
  per completed window *after* its state write; the callback must be
  non-blocking and thread-safe, and a UI delivery failure must not fail the
  sync. `rebuild`/`browserless_export` report start/finish only.
- Jobs run on daemon worker threads: Ctrl-C exits the server; in-flight network
  work is abandoned, not cancelled, and the UI says so. No cancel button in v1.
- On completion the server re-reads shaped summary and pushes refreshed
  fragments.

SSE contract:

- Bridge worker→loop with `loop.call_soon_threadsafe(queue.put_nowait, ...)`
  (loop captured at startup); never a blocking `queue.get()` in the async
  generator.
- One bounded queue per connected client; progress events coalesce when a
  queue is full. Every connect/reconnect first receives an authoritative
  full-state snapshot, so missed events cannot wedge the UI (EventSource
  auto-reconnects).
- Keepalive comment every ~15 s; disconnected clients cleaned up; subscriber
  count capped.
- SSE responses: `Cache-Control: no-store, no-transform`, no compression. v1
  ships **no** `GZipMiddleware` at all (loopback bandwidth is free; Starlette's
  gzip cannot exclude the SSE path and buffers streams).
- Datastar protocol events (`datastar-patch-elements`/`-signals`) are emitted
  via the `datastar-py` SDK, not hand-formatted; a protocol-compat test pins
  SDK ↔ vendored-JS agreement.

## 8. Security & privacy

Threat model note: loopback is not a privacy boundary. Any web page the user
visits can send cross-origin POSTs to localhost, and DNS rebinding lets a
malicious page read localhost responses same-origin. Given the payload here is
complete location history, v1 ships the following, not as hardening backlog but
as part of the phases that introduce each surface:

- **Host allowlist** (DNS-rebinding defense): reject any request whose `Host`
  is not `127.0.0.1:PORT` / `localhost:PORT` / `[::1]:PORT`
  (`TrustedHostMiddleware` or equivalent). Phase A.
- **Cross-site request defense** on all POSTs (Phase B): reject unless
  `Sec-Fetch-Site` is `same-origin`/`none`, require a custom request header
  minted per process (forces preflight an attacker cannot pass), require
  `application/json`. No permissive CORS configuration anywhere. Token never in
  URLs or logs.
- **Escaping**: Jinja2 autoescape + `StrictUndefined`; no `Markup`/`|safe` on
  artifact-derived text ever. All Garmin/GPX/KML strings (message text, names,
  folder names, parse errors) are untrusted. Adversarial fixtures (tags,
  quotes, Unicode, malformed JSON) asserted to render inert. Map popups via
  `textContent`.
- **CSP** (single test-locked header): `default-src 'none'`;
  `script-src 'self'` (+ `'unsafe-eval'` only if pinned Datastar requires it —
  verified during implementation, each relaxation commented);
  `style-src 'self'`; `connect-src 'self'`; `img-src 'self' data: blob:`;
  `worker-src blob:` (MapLibre CSP bundle); `object-src 'none'`;
  `base-uri 'none'`; `frame-ancestors 'none'`; `form-action 'self'`.
- **Headers everywhere**: `Cache-Control: no-store` on all HTML/API/error
  responses (immutable caching only on content-hashed static);
  `Referrer-Policy: no-referrer`; `X-Content-Type-Options: nosniff`;
  `Cross-Origin-Resource-Policy: same-origin`.
- **Data minimization**: shaped responses only (no raw `summary.json`
  passthrough); per-layer property allowlists; paths rendered relative to
  `data_dir` or as basenames; `feed_url`/identifier/IMEI never rendered or
  broadcast (screenshots of this UI end up in bug reports).
- **Logging**: access/exception logs never include query strings, bodies,
  coordinates, message text, identifiers, IMEIs, feed URLs, or env values.
  `debug=False` always — no traceback pages.
- **Path traversal**: registry allowlist + no-`/`-after-decoding check +
  `resolve()`/`is_relative_to()` belt-and-braces (Starlette decodes percent
  escapes into path params).

## 9. Testing

`starlette.testclient` (httpx-backed; httpx already core) with synthetic
fixtures. Beyond the obvious happy paths:

- CLI: dispatch regression (`serve` triggers no rebuild), `--help` and core
  commands work without the `ui` extra, missing-extra error message.
- Security: Host-allowlist rejection, `Sec-Fetch-Site`/custom-header/CSRF
  rejection paths, traversal attempts (`../`, absolute, percent-encoded),
  security-header and CSP string assertions, stored-XSS fixtures for messages,
  names, and parse errors.
- Artifacts: missing/corrupt/intermediate-shape `summary.json`, absent
  geojson capability (`--formats gpkg` build), absent `messages` layer,
  read-during-`_replace_directory` (Windows-focused: the repo's primary
  platform), oversized and malformed layer files.
- Jobs/SSE (Phase B): single-flight 409, second request served while a job
  runs (event-loop non-blocking), multi-client fan-out, reconnect snapshot,
  queue overflow coalescing, worker exception → `failed`, partial_success,
  shutdown during a job.
- Packaging: built wheel installed into a clean venv serves every static asset
  via `importlib.resources`; vendored licenses present; datastar SDK↔JS
  version-agreement test.
- One manual browser smoke check per phase (network panel: zero non-local
  requests; map renders; Datastar morphs preserve the map island).

Quality gates remain `uv run ruff format --check`, `uv run ruff check`,
`uv run pytest`. Test data synthetic per project policy.

## 10. Performance

Large `track_points.geojson` is served as a single in-memory response (no
ranges — MapLibre downloads and parses whole GeoJSON documents regardless; no
gzip in v1). Before any optimization work, benchmark representative synthetic
data (bytes, feature count, load and interaction latency). Likely follow-up is
PMTiles or vector tiles, which MapLibre consumes natively with spatial
filtering; FlatGeobuf is **not** a native MapLibre source and would need a
custom protocol reader. Backlog item, decided by the benchmark.

## 11. Phasing (maps to beads)

0. **CLI dispatch refactor** — explicit per-command dispatch, regression tests.
   Independent value; prerequisite for everything below.
1. **Foundation** — `ui` extra + lock regen, `serve` command (loopback-only,
   startup ergonomics), `create_app`, artifact adapter + capability model +
   layer registry, Jinja2 environment, security headers + Host allowlist,
   vendored assets + `VENDORED.md` + packaging tests, dashboard from shaped
   summary with freshness states.
2. **Read-only views** — messages timeline (ordering/paging/cache), map island
   (CSP bundle, basemap-free style, property allowlist, fit-to-data). Includes
   the small exporter change adding per-layer `bbox` to `summary.json`.
3. **Job services** — shared CLI/UI orchestration (`run_*`), interprocess
   writer lock, typed progress callback in `sync_mapshare`, CSRF defenses,
   build/mapshare/explore buttons with capability detection.
4. **SSE live updates** — event bus per the §7 contract, live dashboard
   refresh.
5. **Backlog (measured)** — benchmark; PMTiles/vector-tiles decision;
   optional `--tile-url` basemap opt-in.

Definition of done for every phase includes updating `Tech_dev.md` (repository
map, runtime data layout, test count) and `Readme.md` where user-facing.

## 12. Resolved questions

- **Templating**: Jinja2, autoescape + `StrictUndefined` (both reviewers;
  security-decisive, not taste).
- **Stale outputs**: hint only, with the exact command; "Rebuild" becomes a
  button in Phase 3. Never auto-build (empty-archive `RuntimeError`, unbounded
  startup work).
- **datastar-py**: `>=1.0.2,<2`; vendored JS at exactly the locked version;
  agreement test; re-verify versions immediately before implementation (young
  dependency).
