# Vendored static assets

Assets are loaded via `importlib.resources` and shipped in wheels/sdists.
Filenames are content-hashed (first 8 hex chars of the SHA-256), so a version
bump always changes the URL and immutable cache headers stay safe.

## datastar-2837d87a.js

- Project: Datastar (https://data-star.dev)
- Version: 1.0.2 (must match the locked `datastar-py` version; a test asserts
  the `// Datastar v…` header line agrees with `importlib.metadata`)
- Source URL: https://cdn.jsdelivr.net/gh/starfederation/datastar@v1.0.2/bundles/datastar.js
- SHA-256: 2837d87acf6ee0ba8e4e63765926c25a98d63883b02f88be194a86b81d3fd24a
- License: MIT (see LICENSE-datastar.md, from
  https://raw.githubusercontent.com/starfederation/datastar/v1.0.2/LICENSE.md)
- Retrieved: 2026-08-16

## maplibre-gl-csp-a1f1847b.js, maplibre-gl-csp-worker-f950e7b1.js, maplibre-gl-ab1e70d5.css

- Project: MapLibre GL JS (https://maplibre.org)
- Version: 5.24.0 (the CSP bundle; MapLibre 6.x is ESM-only and no longer
  ships `maplibre-gl-csp*.js`, so 5.x is a deliberate choice — revisit when
  adopting an ESM loading strategy)
- Source URLs:
  - https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl-csp.js
  - https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl-csp-worker.js
  - https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.css
- SHA-256:
  - maplibre-gl-csp-a1f1847b.js: a1f1847bac64aa00acbf80fbb79b2c5af24d8eecaaa5e2ad14080fab81f1de95
  - maplibre-gl-csp-worker-f950e7b1.js: f950e7b15c49c8b9c7bb52136df2a2df2f6f03b83b8927774573fc98b067f7f0
  - maplibre-gl-ab1e70d5.css: ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732
- License: BSD-3-Clause (see LICENSE-maplibre.txt, from
  https://unpkg.com/maplibre-gl@5.24.0/dist/LICENSE.txt)
- Retrieved: 2026-08-16
- The worker URL is set explicitly at map init
  (`maplibregl.setWorkerUrl("/static/maplibre-gl-csp-worker-f950e7b1.js")`),
  which is what the CSP bundle exists for.

## Update procedure

### Datastar

1. Bump the exact `datastar-py==<version>` pin in `pyproject.toml` (both the
   `ui` extra and the `dev` group -- always in lockstep with the vendored JS
   below), run `uv lock`.
2. Download the matching `bundles/datastar.js` for the same tag, compute its
   SHA-256, and save it as `datastar-<first8>.js`; delete the old file.
3. Update this file (version, URL, SHA-256, retrieval date) and refresh
   LICENSE-datastar.md from the same tag.
4. Run the test suite: the version-agreement and static-serving tests fail on
   any mismatch.

### MapLibre GL JS

1. Pick the new `maplibre-gl` tag (see the version note above for the 5.x vs.
   6.x/ESM-only caveat).
2. Download `dist/maplibre-gl-csp.js`, `dist/maplibre-gl-csp-worker.js`, and
   `dist/maplibre-gl.css` for that tag from unpkg.
3. Compute each file's SHA-256, rename it to `<prefix>-<first8>.<ext>`
   (`maplibre-gl-csp-<hash>.js`, `maplibre-gl-csp-worker-<hash>.js`,
   `maplibre-gl-<hash>.css`), and delete the three old files.
4. Update this file (version, URLs, all three SHA-256 hashes, retrieval
   date) and refresh LICENSE-maplibre.txt from the same tag.
5. Run the test suite: the vendored-asset pin tests and static-serving tests
   fail on any mismatch.
