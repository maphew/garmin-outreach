"""Local web UI for browsing garmin-outreach pipeline outputs.

This package is split so that `garmin_outreach.serve.artifacts` (the
read-only adapter over `data/` outputs) imports cleanly without the
optional `ui` extra installed. Third-party imports (starlette/uvicorn/
jinja2) and the `create_app()` / `run()` entry points live in
`garmin_outreach.serve.app`; import from there, not from this package.
"""
