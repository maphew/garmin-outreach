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

## Update procedure

1. Bump `datastar-py` in `pyproject.toml` (both the `ui` extra and the `dev`
   group), run `uv lock`.
2. Download the matching `bundles/datastar.js` for the same tag, compute its
   SHA-256, and save it as `datastar-<first8>.js`; delete the old file.
3. Update this file (version, URL, SHA-256, retrieval date) and refresh
   LICENSE-datastar.md from the same tag.
4. Run the test suite: the version-agreement and static-serving tests fail on
   any mismatch.
