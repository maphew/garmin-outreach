# Design review: `garmin-outreach serve` (Claude reviewer)

Reviewed: 2026-08-15
Spec: `docs/spec-serve-ui.md` (draft dated 2026-08-15)
Reviewer: Claude reviewer agent (opus tier, read-only), spawned from the drafting session.
Companion review from a second model family: `spec-serve-ui-review-codex.md`.

## Verdict

The spec is well-structured and mostly accurate about the codebase, and the phasing is genuinely independently landable. But there are four things I'd call blockers before implementation starts: a CLI dispatch bug the new subcommand walks straight into, a Windows file-locking interaction between HTTP reads and the exporter's directory swap, an unaddressed CSRF/DNS-rebinding threat model that "loopback only" does not cover, and a CSP + GZip + SSE combination that as written cannot work. Details below, ranked within each requested category.

---

## 1. Factual claims about the existing code

**F1 — `serve` will fall through into `rebuild()` — blocks implementation.**
`src/garmin_outreach/cli.py:165`:
```python
if args.command == "build" or not getattr(args, "no_build", False):
    result["output"] = rebuild(args.data_dir, formats=formats, gap_hours=args.trip_gap_hours, ...)
```
A `serve` subparser has no `no_build` attribute, so `getattr(...) is False` → `not False` → **the branch runs**, and then `args.trip_gap_hours` raises `AttributeError` (not caught by the `except (RuntimeError, ValueError)` at `cli.py:174`, so the user gets a traceback after the server exits). Spec §5 never mentions this. Fix: change the guard to an explicit allowlist, e.g. `if args.command == "build" or (args.command in {"ingest", "mapshare", "explore"} and not args.no_build)`, and add a regression test that `garmin-outreach serve --help` and a dispatch dry-run touch no pipeline code.

**F2 — "artifacts the pipeline already writes atomically (`os.replace`)" is only half true — blocks implementation (drives F5/F6).**
Spec §6 bullet 2. True for `summary.json` (`pipeline.py:51-54`, `exporters.py:107,175-178`), `mapshare-state.json` (`mapshare.py:163-167`), and raw archives (`archive.py:17-19`). **Not** true for the GeoJSON/Shapefile trees: `exporters.py:131-146` `_replace_directory()` does a three-step swap (`destination.rename(old)` → `temp.rename(destination)` → `rmtree(old)`). Between steps 1 and 2 `data/output/geojson/` **does not exist**, so a concurrent `GET /api/layers/x.geojson` legitimately sees `FileNotFoundError`, and a directory listing used to build the layer allowlist sees nothing. The spec must state that layer reads retry-once/degrade gracefully rather than 500.

**F3 — `summary.json` is written twice per rebuild, with different shapes — should-fix.**
`exporters.py:107` writes `{feature_count, layers, formats, written}`; `pipeline.py:49-54` then rewrites the same path with `input_files` and `parse_errors` added. So during a rebuild there is a real window where `summary.json` parses fine but **lacks `input_files` and `parse_errors`**. Spec §7 ("Dashboard content, from real fields") reads as if those keys are always present. Require `.get()` with defaults for every field, and add a test fixture for the intermediate shape, not just the missing-file case.

**F4 — messages field names are wrong/unverifiable — should-fix.**
Spec §7: "`geojson/messages.geojson` (already includes text, timestamp, type per the layer contract)". Actual normalized keys are `text`, `timestamp_utc`, and `event` (`parsers.py:247-248,255-256`); `type` exists only on GPX-derived points (`parsers.py:336`), not on MapShare KML messages. Also there is **no "layer contract" document** in the repo — `Tech_dev.md:200-219` documents layer *classification*, not per-layer fields. Two further consequences the spec should name: a column is absent entirely from the GeoJSON if no feature in that layer populated it (columns come from the GeoDataFrame union, `exporters.py:60-67`), and `messages.geojson` does not exist at all when no feature classified as `messages` (only non-empty layers are written, `exporters.py:56`, confirmed at `Tech_dev.md:241`).

**F5 — "fastapi.testclient (httpx, already a core dependency)" is true but insufficient — blocks implementation.**
httpx 0.28.1 is locked (`uv.lock:214`) and `anyio` is present, so TestClient's transitive needs are met — but `import fastapi.testclient` still needs the `ui` extra, and CI runs `uv sync --locked` with **no extras** (`.github/workflows/ci.yml`), then `uv run pytest`. `tests/test_serve.py` would be a collection error on every CI job. Spec §9's "Existing quality gates unchanged" is therefore wrong. Pick one and say so: (a) add fastapi/uvicorn to the `dev` dependency group, or (b) `pytest.importorskip("fastapi")` at module top and add `--extra ui` to a CI job. Also note the mechanical step the spec omits: adding an extra requires regenerating `uv.lock` (`uv lock`), or `uv sync --locked` fails.

**F6 — "identifier from env/config as CLI does" — there is no config — nitpick.**
Spec §7 Phase 2. Grep confirms the only sources are `GARMIN_MAPSHARE_ID` / `GARMIN_MAPSHARE_USERNAME` / `GARMIN_MAPSHARE_PASSWORD` (`cli.py:42,52,132`); no config file subsystem exists. Say "env only", and specify the UI behaviour when `GARMIN_MAPSHARE_ID` is unset (button disabled with the env-var name shown). Worth adding: `mapshare-state.json` already stores `feed_url` (`mapshare.py:74`), and `feed_url_for()` (`mapshare.py:89-103`) re-validates a full URL against the host allowlist, so the last-used feed is a safe fallback identifier.

**F7 — "uvicorn handles ranged/streamed responses" — wrong attribution — nitpick.**
Spec §10. uvicorn is the ASGI server and does nothing about Range; Range support for `FileResponse` is a Starlette-version-dependent feature, and it is mutually exclusive with `GZipMiddleware` anyway. Drop the claim; say "served as a single streamed response" instead.

**F8 — "datastar.js (~11 kB)" — unverifiable — nitpick.**
Also missing: MapLibre GL JS is roughly 1 MB minified, which is a notable addition to a repo whose largest current file is `uv.lock`. Say the actual committed byte sizes in `VENDORED.md`, and vendor the upstream licence files (MapLibre BSD-3-Clause, Datastar MIT) alongside — redistributing them in the wheel requires it.

---

## 2. Architectural risks

**A1 — Windows: an in-flight HTTP read of an output file will make a concurrent `build` job fail — blocks implementation.**
`exporters.py:140` does `destination.rename(old)` on the `geojson/` **directory**. On Windows a directory rename fails with `PermissionError`/sharing violation if any file inside it is open. Starlette's `FileResponse` holds an open handle for the whole duration of a stream — and §10 explicitly anticipates a large `track_points.geojson`. So: user clicks the map (long stream) → clicks Rebuild → rebuild dies partway, and worse, `_replace_directory`'s `finally` may leave `.geojson-old` behind. Same hazard, smaller window, for `os.replace` over `summary.json` while a reader holds it open. This is the single most likely "works on my Linux CI, breaks on the maintainer's Windows box" defect, and the repo is Windows-primary. Concrete fixes to specify: read layer files fully into memory (or `shutil.copy` to a temp file) before responding rather than streaming an open handle; wrap all state reads in a small retry helper; and have the job runner refuse to start while a layer stream is in flight, or accept the failure and surface it clearly.

**A2 — Blocking `queue.get()` inside an async SSE generator stalls the whole event loop — blocks implementation.**
Spec §6/§7 say "worker thread" + "SSE event bus" without naming the bridge. The naive `threading.Queue.get()` inside `async def event_stream()` freezes every other request on the single-threaded loop. Specify: worker thread publishes via `loop.call_soon_threadsafe(asyncio_queue.put_nowait, event)` (capture the loop with `asyncio.get_running_loop()` at app startup), or the generator uses `await asyncio.to_thread(queue.get, timeout=...)`. Add a test that a second request is served while a job is "running".

**A3 — SSE fan-out, reconnect, and disconnect are unspecified — should-fix.**
Three concrete failures with a single shared queue: (a) two open tabs → each event is consumed by exactly one tab, so the other's dashboard silently desyncs — you need per-subscriber queues; (b) EventSource auto-reconnects, and the reconnecting client misses everything sent in the gap, so a job that finished during the gap leaves the UI stuck on "running" forever — keep a bounded per-job event log and replay it on connect (or make every SSE payload a full state snapshot, which is simpler and idempotent); (c) a disconnected client's generator is not cancelled until the next write, so idle subscribers leak — emit a keepalive comment every ~15 s and cap concurrent subscribers.

**A4 — `GZipMiddleware` + SSE is a known-broken combination — blocks implementation.**
§10 says "enable gzip via `GZipMiddleware`" and §7 adds an SSE endpoint. Starlette's `GZipMiddleware` has no path exclusion and buffers streaming output through a `GzipFile` without per-chunk flush, so SSE frames arrive late or not at all and the UI appears frozen. Specify a `minimum_size` plus explicit exclusion of `/api/events` (custom middleware or per-response `Content-Encoding: identity`), and set `Cache-Control: no-cache` and `X-Accel-Buffering: no` on the SSE response.

**A5 — Single-flight is process-local and does not protect the actual invariant — should-fix.**
§7 says "the pipeline and state files are not designed for concurrent writers", then guards only with a process-level lock. Nothing stops the operator from running `uv run garmin-outreach mapshare` in a terminal while the server runs a job — the far more likely collision, given this is a CLI-first tool whose UI is a convenience layer. Either implement an advisory lock file under `data/` that the CLI also honours (a bigger change, worth its own bead), or state plainly in the spec and in the UI that concurrent CLI use is unguarded. Also pin the deployment shape: `uvicorn` with `workers=1`, `reload=False`, since a module-level lock is meaningless across worker processes.

**A6 — Jobs cannot be cancelled and can outlive Ctrl-C — should-fix.**
`sync_mapshare` can run for many minutes (31-day windows over 15 years of history, plus `time.sleep(2**attempt)` retries at `mapshare.py:113,129` and a 120 s HTTP timeout at `mapshare.py:54`) with no cancellation hook. Specify daemon threads so Ctrl-C actually exits, document that in-flight network work is abandoned rather than cancelled, and make the UI say so.

**A7 — `progress` callback thread-safety and content — should-fix.**
The proposed `progress: Callable[[str], None]` on `sync_mapshare` is invoked from the worker thread; the spec must state the callback must be non-blocking and thread-safe (see A2). Content rule: the natural progress string is the window plus `feed_url`, and `feed_url` embeds the MapShare identifier (`mapshare.py:93`) — see S4.

**A8 — Per-`data_dir` app factory: resolve once — improvement.**
`create_app(data_dir)` should `Path(data_dir).resolve()` at construction and store the resolved geojson dir, so every later join is against a fixed absolute base and `is_relative_to()` checks are meaningful. Also decide the behaviour when `data_dir` doesn't exist at all (first-run): the spec covers "missing `summary.json`" but not "missing `data/`", and `rebuild()` raises `RuntimeError` on an empty archive (`pipeline.py:28`) — so an auto-build-on-startup would crash the server (see Q2).

**A9 — `--formats` may have excluded geojson — should-fix, and it is a whole-feature failure.**
`--formats` is global (`cli.py:24-28`) and a user can legitimately run `--formats gpkg`. Then `data/output/geojson/` does not exist and *every* view in this spec is empty, with no explanation. Spec must define this state: detect it from `summary["formats"]` / `summary["written"]` and render "outputs were built without GeoJSON; run `garmin-outreach --formats gpkg,geojson,shp build`".

**A10 — Simpler alternative: Starlette instead of FastAPI — improvement.**
Nothing in §7 uses FastAPI's differentiators — no request-body models, no validation, no OpenAPI, no DI. FastAPI drags in pydantic + `pydantic-core` (a compiled, platform-specific wheel) purely for routing sugar, on a project whose stated design intent is "self-contained and lightweight". `starlette` + `uvicorn` gives the same routing, `StreamingResponse`, `FileResponse`, middleware, and `starlette.testclient.TestClient` (same httpx-backed API, so §9 is unaffected). Recommend `ui = ["starlette>=0.40,<1", "uvicorn>=0.30,<1"]`. Deletes a dependency subtree and shrinks the lock.

---

## 3. Security & privacy

**S1 — CSRF: any website the user visits can trigger jobs — blocks implementation (Phase 2).**
§7's `POST /api/jobs/*` with no auth and no origin check is reachable from any page in the user's browser via a plain cross-origin HTML form POST (a CORS "simple request": the browser sends it, the attacker can't read the response, but the side effect happens). Consequences here are not cosmetic: `POST /api/jobs/explore` fires a Garmin request, and §7 itself flags that bursts get the account **tarpitted by Cloudflare** (`explore_http.py:30-34,61-66`). Mitigations to specify: reject any request whose `Sec-Fetch-Site` is not `same-origin`/`none`, require a custom header (e.g. `X-Requested-With`, which forces a preflight the attacker can't satisfy), or a per-process token minted at startup. Cheap, and it should be in v1 alongside the buttons, not deferred.

**S2 — DNS rebinding: loopback is not a privacy boundary — blocks implementation.**
A malicious page with a short-TTL hostname rebound to `127.0.0.1` reads `GET /api/layers/track_points.geojson` **same-origin** — i.e. the user's complete location history — despite the server never leaving loopback. §8's "loopback-only by default" doesn't address this. Fix is one middleware: reject any request whose `Host` header is not in `{127.0.0.1:PORT, localhost:PORT, [::1]:PORT}` (plus whatever `--host` was explicitly given). Add `Cross-Origin-Resource-Policy: same-origin` while you're there. Given the sensitivity level this repo already asserts (`Security.md:17-24`, `Tech_dev.md:97`), this is proportionate.

**S3 — `default-src 'self'` as written breaks both Datastar and MapLibre — blocks implementation.**
Datastar evaluates `data-*` expressions via the `Function` constructor, which CSP blocks without `script-src 'unsafe-eval'`; MapLibre GL constructs its worker from a blob URL, needing `worker-src blob:` (and `img-src data: blob:` for canvas/glyph handling). Ship the CSP with these three relaxations spelled out and a comment explaining each, and keep a test asserting the header string so it can't silently widen — that preserves §8's stated intent (a regression guard on the vendoring rule) without a header that forces the implementer to quietly delete it on day one.

**S4 — logging/URL rule is narrower than the sensitive set — should-fix.**
§8 says logs must not print message text or coordinates. Extend to: MapShare identifier and `feed_url` (`mapshare.py:93`, stored in `mapshare-state.json`), IMEI (`cli.py:51`, and `imei` is a feature property, `parsers.py:242`), and device names. Two structural rules matter more than the prose rule: (a) never put message text, coordinates, or identifiers in a **URL** — uvicorn's access log records the full path and query, and so does browser history, so message search/paging must use opaque ids or POST bodies; (b) run with FastAPI/Starlette `debug=False` so tracebacks (which can embed row content) are never rendered to the page. Also consider redacting the identifier when rendering `feed_url` on the dashboard, since screenshots of this UI are exactly what ends up attached to a bug report.

**S5 — Path traversal: name the mechanism and the allowlist source — should-fix.**
§7's "validated against the known layer list" is right but under-specified in two ways. Mechanism: Starlette matches `{name}` on the *undecoded* path segment, then percent-decodes into the path param, so `%2e%2e%2f` can become `../` **inside** `name` — a `"/" not in name` check on the decoded value is the thing that must exist, not just a comment. Source of the list: don't derive it from `summary["layers"]` (data-derived) or from a directory listing (transiently empty, see F2/A1) — hardcode the closed set the parsers can actually produce: `track_points, messages, waypoints, events, tracks, routes, courses, trips` (`parsers.py:286-297,300-306`, `cleanup.py:91`). Then `resolve()` + `is_relative_to(geojson_dir)` as belt-and-braces. Keep the §9 traversal tests.

**S6 — non-loopback bind deserves more than a printed warning — improvement.**
A warning scrolls past. Suggest requiring an explicit second flag (`--allow-remote`) for any non-loopback `--host`, and refusing `0.0.0.0` outright with the Tailscale/WireGuard guidance §5 already contains. Also note `--host ""`/`::` variants when writing the loopback check.

**S7 — `GET /api/summary` passthrough leaks absolute filesystem paths — nitpick.**
`summary["written"]` holds `str(destination)` (`exporters.py:89,94,99`), which is absolute whenever `--data-dir` was absolute — so the user's home directory / username lands in the page and in screenshots. Render basenames, or strip to paths relative to `data_dir`.

---

## 4. Missing or underspecified requirements

**M1 — There is no basemap, and the spec never says so — blocks implementation (Phase 2 scope surprise).**
§2 promises "an interactive map" while also promising "zero external network requests from the browser". Those are only compatible with a blank background: MapLibre renders vector/raster tiles from a tile source, and there is no vendored, offline tile source here. The user will get colored lines on white. Decide and write it down: v1 ships a no-basemap style (defensible, and honest about privacy), with an optional documented `--tile-url` opt-in that also relaxes `img-src`/`connect-src` in the CSP and carries an explicit "this sends your viewport to a third party" warning. This is the requirement most likely to produce "that's not what I expected" on first demo.

**M2 — No initial map extent — should-fix.**
`summary.json` has no bbox (`exporters.py:101-106`), so `/map` has nothing to fit the view to without downloading a layer first. Either compute the bbox client-side after the first layer loads (simple, but janky on a 100 MB file) or add a `bbox` per layer to `write_outputs`' summary (a few lines in `exporters.py`, benefits the CLI too). Prefer the latter and make it its own small bead.

**M3 — Messages paging is unspecified and will re-parse the whole file per page — should-fix.**
§7 says "newest first, paged" over `messages.geojson`. There is no ordering guarantee in the file (features are sorted by `(layer, stable_id)`, `model.py:51`), and some messages have no `timestamp_utc`, so "newest first" needs a defined tiebreak and a defined bucket for undated rows. Specify a parse-once cache keyed on `(mtime_ns, size)` of the file, invalidated on job completion.

**M4 — Startup ergonomics — improvement.**
Unspecified: what happens when the port is in use (want a clear message, not an `OSError` traceback); `--open` racing the server's readiness (open from a startup hook, not before `uvicorn.run`); and the fact that `serve` breaks the CLI's stdout contract — every other command prints a JSON result (`cli.py:173`), so `serve` should keep stdout clean or emit a single JSON line and send all human logs to stderr.

**M5 — Mirror the extra's error wording precisely — nitpick.**
The existing pattern is a `RuntimeError` raised inside the command function, caught by `cli.py:174-176` → exit 2: see `explore.py:67-71` and `browser_cookies.py:129-132`. Both say `pip install -e .[browser]`, while §2 advertises `uv run --extra ui`. Pick one phrasing for the new message (I'd include both: `uv sync --extra ui` for this repo's documented workflow, `pip install -e .[ui]` otherwise) and consider aligning the existing two in the same PR.

**M6 — `Tech_dev.md` updates are not in the phase list — nitpick.**
`Tech_dev.md:71-86` (repository map), `:99-116` (runtime data layout), and `:293-299` ("Before handing off": update the test count and last-verified date) all need edits. §11 should name the doc updates as part of each phase's definition of done, since this repo treats `Tech_dev.md` as the maintainer contract.

---

## 5. Section 12 open questions — recommendations

**Q1 — Templating: use Jinja2 (autoescape on), not stdlib string composition.**
This is a correctness call, not a taste call. Every page in §7 interpolates attacker-influenceable content: inReach message `text` (anyone who can message the device), device/waypoint `name`, and `parse_errors` strings, which embed raw filesystem paths and parser exception text (`pipeline.py:35`). f-strings and `string.Template` do not escape, so one missed call site is stored XSS on a page that (Phase 2) also holds job-triggering POST endpoints — S1 and an XSS compound badly. Jinja2 is pure Python, no C extension, ~1 transitive dep (MarkupSafe), and gives escape-by-default. If the "minimal deps" instinct wins anyway, then make the mitigation structural rather than aspirational: a single `esc()` choke point, plus a lint/test that fails on any `<` … `{` interpolation that didn't go through it. Jinja2 is the smaller total cost.

**Q2 — Stale outputs: hint only. Agreed with the draft, and there's a hard reason beyond aesthetics.**
`rebuild()` raises `RuntimeError` when the archive is empty (`pipeline.py:28`), so auto-build at startup turns "first run, nothing synced yet" into a server that won't start. It's also unbounded work (full reparse of every raw file, plus MapShare seen-ID reconstruction, `Tech_dev.md:267`) triggered by merely opening a browser tab. Hint only, showing the exact command to copy in Phase 1, promoted to a "Rebuild" button in Phase 3. Keep the mtime comparison cheap — cache the `data/raw/**` scan for a few seconds rather than walking the archive on every dashboard render.

**Q3 — datastar-py: resolve the pin at implementation time, pin it exactly, and seriously consider dropping the dependency.**
Do not carry `>=0.4,<1` into code: Datastar went 1.0 and renamed both the client attribute API and the SSE event names (the `datastar-fragment`-era names are gone), so a `<1` range will pull a version whose wire protocol doesn't match a v1 vendored `datastar.js`. Two requirements: (a) pin `datastar-py==X.Y.Z` exactly and record the matching `datastar.js` version in `VENDORED.md`, with a test asserting the two agree so they can never drift; (b) check first whether you need it at all — if the only thing used is SSE frame formatting, that's ~30 lines of `f"event: ...\ndata: ...\n\n"` and dropping it makes the extra `starlette + uvicorn` only (see A10). Take the dependency only if its FastAPI/Starlette response helpers earn their keep; verify the actual module surface of the pinned version rather than assuming it.

---

## Things that are simply right

`os.replace` atomicity for `summary.json` and `mapshare-state.json`; the read-only-first phasing; the non-goals list (especially no Playwright from the UI and no `data/raw/` routes); the manual-only, no-retry, no-poll rule for the Explore job, which correctly reflects `explore_http.py:30-34`; env-var-only MapShare password matching `cli.py:129-133`; and the §9 test list, which targets the right cases (traversal, missing summary, single-flight 409). Fixing F1, A1, A2/A4, S1/S2, S3, and M1 would leave a spec I'd be comfortable handing to a builder.
