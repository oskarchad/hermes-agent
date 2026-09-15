#!/usr/bin/env bash
# Copy into the owning profile's scripts/; use the installed CLI and its secret scope.
set -euo pipefail
: "${HERMES_HOME:?Set HERMES_HOME to the owning profile home}"
exec hermes radar-observer --state-path "${HERMES_HOME}/cron/discord-now-state.json" "$@"
