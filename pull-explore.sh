#!/usr/bin/env bash
# Pull your Garmin Explore history (KML + GPX) with one short command.
#
# Wraps:
#   uv run --extra browser garmin-outreach explore --browser firefox --export-formats both
#
# Any extra arguments are forwarded and override the defaults (argparse keeps the
# last value it sees), so:
#   ./pull-explore.sh                   # Firefox session, KML+GPX, then rebuild GIS outputs
#   ./pull-explore.sh --no-build        # export + archive only, skip the rebuild
#   ./pull-explore.sh --browser chrome  # read the saved session from Chrome instead
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --extra browser garmin-outreach explore --browser firefox --export-formats both "$@"
