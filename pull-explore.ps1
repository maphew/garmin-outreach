#!/usr/bin/env pwsh
# Pull your Garmin Explore history (KML + GPX) with one short command.
#
# Wraps:
#   uv run --extra browser garmin-outreach explore --browser firefox --export-formats both
#
# Any extra arguments are forwarded and override the defaults (argparse keeps the
# last value it sees), so:
#   ./pull-explore.ps1                   # Firefox session, KML+GPX, then rebuild GIS outputs
#   ./pull-explore.ps1 --no-build        # export + archive only, skip the rebuild
#   ./pull-explore.ps1 --browser chrome  # read the saved session from Chrome instead
$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    uv run --extra browser garmin-outreach explore --browser firefox --export-formats both @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
