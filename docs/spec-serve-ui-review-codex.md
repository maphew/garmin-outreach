# Design review: `garmin-outreach serve`

Reviewed: 2026-08-15

Spec: `docs/spec-serve-ui.md` (draft dated 2026-08-15)

Code baseline: `91494e6`

## Verdict

The overall direction is architecturally sound: an optional local server, an application factory
bound to one data directory, server-rendered HTML, a deliberately small MapLibre island, and a
read-only-first delivery sequence fit this CLI well. Reusing normalized outputs instead of parsing
KML/GPX in request handlers preserves the acquisition/build separation described in `TECH_DEV.md`.
FastAPI is a reasonable optional host, and Datastar is a reasonable Phase 2 mechanism for
long-running job feedback.

The draft is not implementation-ready. Phase 1 has five design gates: safe rendering, privacy-safe
response contracts, GeoJSON/output availability, an explicit offline map design, and CLI dispatch.
Phase 2 has three additional gates: browser-origin security, cross-process writer exclusion, and
precise acquisition-plus-build job semantics. These are spec changes, not reasons to abandon the
design.

Severity means impact if shipped. "Blocks" means the affected phase should not start until the spec
chooses and documents a solution. "Nice to have" can be resolved during implementation without
changing the architecture.

## Ranked findings

| ID | Severity | Gate | Spec sections | Finding |
| --- | --- | --- | --- | --- |
| F1 | Critical | Blocks Phase 2 and any non-loopback mode | 5, 7, 8 | Unauthenticated POST endpoints are vulnerable to cross-site triggering, and a warning is not an adequate control for network exposure. |
| F2 | Critical | Blocks Phase 1 | 6, 7, 8, 12 | Garmin text, parse errors, and GeoJSON properties are untrusted content. String-composed HTML and popup HTML create a stored-XSS path. |
| F3 | High | Blocks the map portion of Phase 1 | 2, 4, 6, 10 | Vendoring MapLibre code does not make the map offline. The style, basemap tiles, glyphs, sprites, and CSP worker mode are unspecified. |
| F4 | High | Blocks the skeleton | 4, 5, 6 | The current CLI dispatch will rebuild after a new `serve` command exits, and an eager FastAPI import would break the core install. |
| F5 | High | Blocks Phase 2; must be tolerated in Phase 1 | 6, 7 | Outputs are not one atomic snapshot, and a process-local job lock does not exclude another CLI process. |
| F6 | High | Blocks Phase 2 | 2, 7 | MapShare and Explore jobs do not say whether they rebuild. Calling only the named acquisition functions leaves the displayed outputs stale and differs from current CLI defaults. |
| F7 | High | Blocks affected Phase 1 views | 5, 6, 7 | GeoJSON is optional today, only non-empty layers are written, and there is no versioned UI artifact contract. |
| F8 | High | Blocks Phase 1 privacy acceptance | 6, 7, 8 | Raw `summary.json` and GeoJSON passthrough disclose more than the UI needs, including paths, source metadata, device identifiers, and arbitrary preserved fields. |
| F9 | Medium | Nice to have for Phase 1, required before claiming accurate freshness | 6, 7, 12 | Mtime staleness and "sync freshness" are underspecified and conflate MapShare, Explore, ingest, and build state. |
| F10 | Medium | Blocks a robust Phase 2 implementation | 6, 7 | The SSE/event-bus contract lacks reconnect, backpressure, thread handoff, multi-tab, error, and shutdown semantics. |
| F11 | Medium | Required before release packaging | 4, 8, 9 | Vendored-asset licensing, integrity, package inclusion, source-map behavior, cache policy, and a MapLibre-compatible CSP are missing. |
| F12 | Medium | Nice to have for Phase 1; correct the backlog claim now | 7, 10, 11 | HTTP ranges do not make MapLibre incrementally consume GeoJSON, and MapLibre has no native FlatGeobuf source. |
| F13 | Medium | Required before Phase 2 | 7, 9 | Job inputs, result/error schemas, lifecycle, cancellation/shutdown behavior, and optional Explore dependencies are incomplete. |
| F14 | Medium | Required before each phase lands | 9 | Tests omit the highest-risk security, packaging, malformed-artifact, concurrency, and browser-offline cases. |
| F15 | Low | Nice to have | 5, 7 | Browser-open readiness, port collision behavior, timezone/display rules, pagination, accessibility, and map empty/dateline cases need acceptance criteria. |

## Detailed findings and required changes

### F1. Protect browser-origin and network boundaries

Sections 5 and 8 treat loopback binding as sufficient for v1 and treat non-loopback exposure as an
operator choice after a warning. That is acceptable only for a strictly read-only loopback server
with additional host validation. It is not adequate once Section 7 adds state-changing endpoints.
An attacker-controlled web page can submit a simple cross-origin POST to localhost even when it
cannot read the response. DNS rebinding and permissive `Host` handling also weaken the assumption
that a request reaching `127.0.0.1` came from this UI.

Required specification changes:

- Phase 1 should reject non-loopback `--host` values unless a separate, conspicuous remote mode is
  selected. A warning alone should not enable remote serving of location and message history.
- Phase 2 must validate `Host` with Starlette `TrustedHostMiddleware`, validate exact `Origin` for
  unsafe methods, reject cross-site `Sec-Fetch-Site`, require `application/json`, and require a
  per-process CSRF value in a custom header. Do not put the token in a URL or log it. OWASP describes
  Origin checking, Fetch Metadata, and custom headers as complementary CSRF controls in its
  [CSRF guidance](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html).
- Do not configure permissive CORS. CORS is not the primary CSRF defense.
- Remote mode needs authentication and transport security, or it must remain out of scope. "Use a
  VPN" is useful deployment guidance but does not establish which VPN peers may read the data. A
  defensible v1 choice is: remote bind is refused; authenticated reverse-proxy support is a later
  feature. At minimum, action routes must be disabled in unauthenticated remote mode.
- Run exactly one Uvicorn worker with reload disabled. Multi-worker operation defeats an in-memory
  lock and event bus.

This blocks Phase 2 and any non-loopback release.

### F2. Treat all artifact content as untrusted

Sections 6, 7, and 12 assume HTML composition is mostly a dependency question. It is also a security
boundary. Imported GPX/KML, Garmin messages, folder names, `extra_json`, `source_file`, and parser
exception text can contain attacker-controlled strings. The messages page is a direct stored-XSS
sink. Map popups become another sink if `setHTML`, `innerHTML`, or template interpolation is used.
A CSP helps but does not replace correct escaping.

Required specification changes:

- Use Jinja2 with autoescape enabled and `StrictUndefined`; never mark artifact text safe. This is
  the decisive answer to the first Section 12 question.
- Build MapLibre popups with DOM nodes and `textContent`, not `setHTML` with interpolated fields.
- Define a small display model with typed/validated fields. Treat invalid timestamps and unexpected
  property types as data-quality warnings, not template exceptions.
- Keep scripts and styles in external files. Add explicit CSP directives, including
  `default-src 'none'`, `script-src 'self'`, `style-src 'self'`, `connect-src 'self'`,
  `img-src 'self' data: blob:`, `worker-src` as selected for MapLibre, `object-src 'none'`,
  `base-uri 'none'`, `frame-ancestors 'none'`, and `form-action 'self'`. `frame-ancestors` and
  `form-action` do not fall back in all useful ways from `default-src`; see the
  [OWASP CSP guidance](https://cheatsheetseries.owasp.org/cheatsheets/Content_Security_Policy_Cheat_Sheet.html).
- Add adversarial fixtures containing tags, attributes, quotes, Unicode, malformed JSON, and popup
  content. Assert they render as text.

This blocks Phase 1.

### F3. Define what the offline map actually displays

Section 4 vendors only MapLibre JS/CSS. Section 2 promises zero browser network requests, but a
normal MapLibre style also refers to a style document, tile sources, glyphs, and sprites. MapLibre's
own quickstart uses a remote style, and its style specification defines URL-loaded
[sources](https://maplibre.org/maplibre-style-spec/sources/),
[glyphs](https://maplibre.org/maplibre-style-spec/glyphs/), and
[sprites](https://maplibre.org/maplibre-style-spec/sprite/).

Choose one map product before implementation:

1. Recommended for v1: an offline, basemap-free map with a local inline style containing only a
   background layer plus the locally served GeoJSON overlays, scale/control UI, and fit-to-data.
   State plainly that roads, terrain, and place labels are absent.
2. Bundle a bounded offline tile/style/glyph/sprite dataset and define its geography, license, size,
   update policy, and attribution. This is much larger than the draft implies.
3. Make an online basemap an explicit opt-in privacy tradeoff. This no longer satisfies the stated
   fully-offline goal and must never be the default.

MapLibre also requires blob workers under its standard bundle. For a strict CSP, vendor
`maplibre-gl-csp.js` and its worker and set the worker URL, as documented in the official
[MapLibre CSP instructions](https://maplibre.org/maplibre-gl-js/docs/). Do not loosen the policy to
`unsafe-inline` or `unsafe-eval` merely to make the default bundle work.

This blocks the map portion of Phase 1.

### F4. Refactor CLI dispatch and keep the optional dependency optional

The proposed subcommand does not fit the current `main()` fallthrough unchanged. At
`src/garmin_outreach/cli.py:165-172`, every command without `no_build=True` invokes `rebuild`.
Consequently, a naïvely added `serve` command would start the server and rebuild after shutdown.
Section 5 also omits how a browser is opened only after the socket is ready.

Required changes:

- Refactor command handling into explicit command branches or command handlers. Only `build`, and
  acquisition/ingest commands without `--no-build`, may rebuild.
- Import the UI runner inside the `serve` branch. `garmin-outreach --help` and every core command
  must work when the `ui` extra is absent. Catch the relevant `ImportError` and name the exact install
  command.
- Resolve `data_dir` once at startup so worker threads and browser-open code do not depend on later
  current-directory changes.
- Specify startup readiness and failures: bind first, open the browser after successful startup,
  report an occupied port clearly, and do not open a tab when app creation fails.
- Add parser and smoke tests both with and without the optional UI dependencies.

This blocks the skeleton.

### F5. Do not claim a globally atomic artifact snapshot

Section 6 says the UI reads artifacts that the pipeline writes atomically. Individual final files
are commonly replaced atomically, but a whole build is not one atomic generation:

- `exporters.py:70-107` replaces GPKG, GeoJSON, and Shapefile sequentially.
- `_replace_directory()` at `exporters.py:131-146` briefly renames the current directory away before
  the new one is renamed into place.
- `write_outputs()` writes `summary.json` at `exporters.py:101-107`, then `rebuild()` writes it again
  with input/error fields at `pipeline.py:48-54`.
- The proposed process-level lock does not exclude `garmin-outreach build`, `mapshare`, `explore`, or
  a second server process.

For Phase 1, specify that readers tolerate missing/replaced files and malformed partial external
state with a bounded retry, returning a friendly 503/empty state rather than 500. For Phase 2, add a
data-directory-scoped interprocess writer lock used by every mutating CLI and UI path. A better
long-term publication model is versioned output directories plus one atomically replaced manifest
that identifies the active generation. This avoids Windows rename/open-handle conflicts and lets
requests finish against an old generation while a new one is built.

Also define whether external CLI writes while `serve` is running are supported. If not, detect and
reject them rather than relying on documentation.

### F6. Make jobs orchestration services, not bare function calls

Current CLI behavior is acquisition followed by `rebuild` unless `--no-build` is given
(`cli.py:116-172`). Section 7 says `/api/jobs/mapshare` calls `sync_mapshare` and
`/api/jobs/explore` calls `browserless_export`, then re-reads `summary.json`. That summary cannot
reflect newly archived files unless a rebuild also occurs.

Specify three application services shared by CLI and UI:

- `run_build(options)`
- `run_mapshare(options)`: acquire, then build on acquisition success
- `run_explore_http(options)`: acquire, then build on acquisition success

Keep `sync_mapshare`, `browserless_export`, and `rebuild` as lower-level operations. Sharing the
orchestration service prevents CLI/UI drift. Define whether a successful acquisition followed by a
failed build is `partial_success`, and preserve the archived raw bytes in that case. Progress should
be a typed event (`stage`, counts, safe message), not an arbitrary string that may later leak URLs,
paths, identifiers, or message content.

The natural MapShare progress boundary is each successfully processed date window around
`mapshare.py:55-80`. Specify that its callback runs only after the corresponding state update, that a
UI delivery failure cannot roll back or fail the sync, and that the returned `feed_url`
(`mapshare.py:47-48, 81-86`) is never broadcast or rendered.

This blocks Phase 2.

### F7. Define and version the UI artifact contract

Section 6 assumes `output/geojson/*.geojson` exists. The global CLI accepts `--formats gpkg` or
`--formats shp` (`cli.py:23-28`), and `write_outputs` creates GeoJSON only when requested
(`exporters.py:91-94`). Only non-empty layers are emitted, so a valid build may have no
`messages.geojson`. The documented message fields are not universal either: GPX-derived messages
may lack text or an event type, while the parser preserves varying properties.

Required changes:

- Add a small artifact adapter/repository between routes and files. It owns allowlisted layer names,
  JSON parsing, schema validation, projection/redaction, and friendly absence states.
- Add a normalized schema version before the UI becomes a downstream consumer. Reject unsupported
  future versions with a useful rebuild/upgrade message.
- Treat `summary.formats` and the actual files as capabilities. If GeoJSON is absent, dashboard
  still works while map/messages show "GeoJSON output not built" and the exact CLI command. Do not
  auto-build in Phase 1.
- Define the canonical layer registry once rather than duplicating parser names in the router.
- Define message ordering and fallbacks for absent/invalid timestamps, text, and type.

This blocks only the views whose artifacts are unavailable, but the contract must be specified
before Phase 1 code is structured.

### F8. Minimize the HTTP data surface and browser persistence

Section 7 proposes `summary.json` and GeoJSON passthrough. `summary.json` contains written paths and
parse errors containing raw input paths (`pipeline.py:31-38, 48-54`). GeoJSON properties can include
`source_file`, IMEI, device name, incident ID, coordinates duplicated as properties, and arbitrary
`extra_json` (`parsers.py:235-270`). The map normally needs much less. Loopback does not make this
data harmless if another local process, malicious page, extension, or accidentally enabled remote
bind reaches the service.

Required changes:

- Replace `/api/summary` passthrough with a shaped response containing counts, sanitized error
  summaries, capabilities, and freshness status. Do not emit filesystem paths or feed URLs.
- Define a property allowlist per displayed layer. If a "download full layer" capability is desired,
  make that explicit and separate from the map endpoint.
- Return `Cache-Control: no-store` on HTML, summary, GeoJSON, SSE, errors, and job responses. Long
  immutable caching belongs only on content-hashed static assets. Add `Referrer-Policy: no-referrer`
  and `X-Content-Type-Options: nosniff`. `no-store` is the appropriate baseline for sensitive
  browser responses; see [MDN caching guidance](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Caching).
- Sanitize user-visible parse errors to basename plus error category by default. Full diagnostic
  paths belong in an opt-in terminal log, still without message/coordinate content.
- Ensure access and exception logs never include query strings, request bodies, feed URLs, CSRF
  values, coordinates, messages, IMEIs, cookies, or environment values.

This blocks Phase 1 privacy acceptance.

### F9. Specify freshness without adding nondeterminism

Section 7's "output versus raw mtime" can be a pragmatic hint because raw archives are append-only,
but the exact rule is missing. `mapshare-state.json:last_success_utc` describes MapShare polling,
not Explore freshness, local imports, or the last successful build. An interrupted MapShare sync can
also leave a valid intermediate watermark.

Recommended definition:

- `outputs_missing`: no readable compatible summary.
- `outputs_stale`: any eligible raw file has mtime newer than the successfully published summary,
  with file count as a secondary check.
- `mapshare_last_window`: parsed `last_success_utc`, labeled specifically as MapShare state.
- `freshness_unknown`: malformed state, clock anomaly, or unsupported summary.

Do not add a wall-clock build timestamp to deterministic GIS output solely for this UI. If stronger
freshness is needed, use a separate operational state file or an input inventory/content digest in
the publication manifest. Add tests for same-timestamp files, clock skew, missing state, corrupt JSON,
and interrupted builds.

### F10. Specify the SSE and thread boundary

Section 7 names an event bus but not its behavior. A worker thread cannot safely push directly to an
`asyncio.Queue`; it must schedule delivery onto the owning event loop. Multiple tabs create multiple
consumers, and an unbounded queue can retain progress forever for a disconnected browser.

Define:

- job IDs and states: `queued` (if retained), `running`, `succeeded`, `partial_success`, `failed`;
- `POST` returns 202 with the accepted job snapshot, or 409 with the current snapshot;
- one bounded queue per connected client, a drop/coalesce rule for progress, and an authoritative
  current-job snapshot sent on connect/reconnect;
- heartbeat interval, disconnect cleanup, and `Cache-Control: no-store, no-transform` for SSE;
- thread-to-loop delivery with `loop.call_soon_threadsafe` or an equivalent anyio primitive;
- exception sanitization, event IDs/reconnect behavior, and whether completed job state survives only
  for the server process lifetime;
- shutdown behavior. Stop accepting jobs, let a bounded operation finish or document forced exit,
  and never claim a running thread was cancelled when Python cannot cancel it safely.

The official Datastar protocol uses `datastar-patch-elements` and `datastar-patch-signals` SSE events;
use the SDK rather than hand-formatting them. See the
[Datastar SSE reference](https://data-star.dev/reference/sse_events).

### F11. Complete the vendoring and packaging contract

Section 4's `VENDORED.md` should record exact upstream version, source URL, SHA-256, license, date,
and update command/process for every asset. Include upstream license/notice files in wheels and
sdists. Verify installed-package loading with `importlib.resources`; do not rely on a source-checkout
path. Remove or vendor any `sourceMappingURL` target and inspect CSS/style files for remote URLs.

Use content-hashed filenames if serving `immutable`; a query string is less robust than a filename
whose content identity is obvious. Add a built-wheel test that installs into a clean environment,
starts the app, and retrieves every asset. Add a browser smoke test that fails any request whose host
is not the local server.

MapLibre's standard CSP needs `worker-src blob:` and `img-src data: blob:`; the official docs also
offer a separate CSP bundle and worker. This is why Section 8's bare `default-src 'self'` policy will
not run the proposed map unchanged.

### F12. Correct the performance assumptions

Section 10 attributes range/stream behavior to Uvicorn. `FileResponse` behavior belongs to
Starlette, and MapLibre's GeoJSON source still downloads and parses the whole GeoJSON document.
Range support therefore does not provide incremental map loading. Gzip reduces bytes over loopback
but adds CPU and does not fix browser parse/render cost. Measure file bytes, feature count, load time,
and interaction latency on representative synthetic data before selecting 100,000 points as a
threshold.

FlatGeobuf is not a native MapLibre source. MapLibre documents core GeoJSON, vector/raster tile,
image, video, and canvas sources, with custom third-party sources possible
([MapLibre source API](https://maplibre.org/maplibre-gl-js/docs/API/interfaces/Source/)). A FlatGeobuf
follow-up therefore needs an additional JS reader/custom protocol and spatial-window loading, not
just an exporter change. Vector tiles or PMTiles may fit MapLibre more directly. Correct the backlog
wording now; the optimization itself remains out of scope.

### F13. Complete job and dependency requirements

Section 7 needs request schemas and bounds for cleanup parameters, MapShare full/start/end/chunk/IMEI
choices, and Explore formats/browser selection. Prefer a deliberately small v1 action surface over
exposing every CLI option. Do not accept a feed URL, identifier, password, or cookie material from
the browser. The draft says "env/config as CLI does", but the CLI has environment variables, not a
general config system. State whether the existing `mapshare-state.json.feed_url` may be reused or
whether `GARMIN_MAPSHARE_ID` is mandatory.

`browserless_export()` needs the saved-browser-cookie dependency supplied by the current `browser`
extra. Installing only `--extra ui` will not make the Explore action usable. Either omit Explore
from the first Phase 2 slice, detect it and show an unavailable capability, document installing both
extras, or split the cookie reader into a smaller optional extra. Also state that one UI job attempt
still performs the existing session check, user preflight, and one export POST per chosen format; it
does not mean one HTTP request.

### F14. Expand verification around actual risks

Keep the Section 9 tests and add:

- Host, Origin, Fetch Metadata, CSRF, content-type, remote-bind, and disabled-action tests;
- stored-XSS fixtures for messages, errors, names, and popup properties;
- no-store/security-header assertions and an offline browser test with all nonlocal requests blocked;
- missing, empty, corrupt, oversized, unsupported-version, and concurrently replaced artifacts;
- builds without GeoJSON and builds without a messages layer;
- a CLI smoke test without `ui`, plus wheel/sdist inclusion and license checks;
- two app instances or an external writer contending for the same data-directory lock;
- multiple SSE clients, disconnect/reconnect, queue overflow, worker exception, partial success, and
  shutdown during a job;
- a Windows-focused read-during-replace test because that is a likely deployment platform here.

`fastapi.testclient` is appropriate for route-level tests. The existing core `httpx` dependency does
cover its HTTP client requirement, but an actual browser smoke test is still needed for MapLibre,
CSP, Datastar DOM preservation, and zero-external-request claims.

### F15. Smaller requirements worth recording

These are nice to have rather than architectural gates:

- Define cursor pagination, maximum page size, stable tie-breaking, missing timestamps, and UTC versus
  local display for `/messages`.
- Make the MapLibre island idempotent across Datastar morphs. Preserve its root DOM node, define
  custom event payloads, and clean up map/worker resources if it is removed.
- Define empty bounds, single-point bounds, antimeridian tracks, invalid geometry, WebGL unavailable,
  keyboard navigation, reduced motion, color contrast, and a non-map tabular fallback.
- Treat port 8477 as a default preference, not a guarantee of availability. Fail clearly or support an
  explicit port 0 mode for tests.

## Answers to Section 12

### 1. Templating choice: use Jinja2

Use Jinja2 in the `ui` extra, with autoescape, `StrictUndefined`, package-resource templates, and no
artifact-derived `Markup`/safe bypass. The extra dependency is small compared with FastAPI and
MapLibre, and escaping correctness matters more than avoiding one dependency. Standard-library
string composition is acceptable only for trivial fixed fragments, not a messages timeline and
repeated SSE fragments containing imported data.

Suggested dependency: `jinja2>=3.1,<4`, resolved exactly in `uv.lock`.

### 2. Stale outputs: hint only

Keep the draft's hint-only behavior. Read-only startup should remain read-only, fast, and useful even
when raw inputs are corrupt or GIS dependencies fail. An automatic startup build would hide an
expensive mutation behind a server command, complicate readiness, locking, errors, deterministic
expectations, and tests. In Phase 2, show a deliberate "Build now" action with the same security and
single-flight controls as other jobs. Never auto-run MapShare or Explore.

The spec must first define the freshness states in F9 and the missing-GeoJSON state in F7.

### 3. `datastar-py` version: the draft range is obsolete

As of 2026-08-15, the current stable Python SDK is `datastar-py` 1.0.2, released 2026-06-02, and the
Datastar browser library's current stable release is also 1.0.2. The draft constraint
`datastar-py>=0.4,<1` excludes the stable major. Sources:
[PyPI release history](https://pypi.org/project/datastar-py/),
[Python SDK releases](https://github.com/starfederation/datastar-python/releases), and
[browser library releases](https://github.com/starfederation/datastar/releases).

Use `datastar-py>=1.0.2,<2` in project metadata and let the committed `uv.lock` provide the exact
development/CI resolution. Vendor the browser asset at exactly 1.0.2, record its digest/license, and
add a small protocol compatibility test. An exact Python `==` pin in project metadata is not needed;
it would unnecessarily block patch fixes. Recheck both versions immediately before implementation
because this dependency is young.

## Recommended revised phase gates

1. **Foundation:** refactor CLI dispatch; lazy-load UI; add artifact adapter, schema/capability model,
   safe Jinja environment, security headers, packaging checks, and loopback/Host enforcement.
2. **Read-only dashboard/messages:** shaped summary, tolerant corrupt/missing states, escaped timeline,
   no-store responses, defined freshness. No raw JSON passthrough.
3. **Offline map:** choose basemap-free or bundled-map behavior; use the CSP MapLibre bundle; project
   allowed properties; test with the network disabled and GeoJSON absent/large.
4. **Job services:** shared CLI/UI orchestration, interprocess writer lock, typed progress, explicit
   result states, CSRF/Origin/Fetch Metadata protection, and disabled unauthenticated remote actions.
5. **SSE:** bounded per-client delivery, reconnect snapshot, shutdown behavior, and multi-client tests.
6. **Measured performance backlog:** choose FlatGeobuf plus a browser reader, vector tiles, or PMTiles
   only after a benchmark identifies the bottleneck.

With these gates incorporated, the design remains a good fit for the repository and can still land
in independently useful slices.
