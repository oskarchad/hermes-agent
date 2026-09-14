#!/usr/bin/env bash
+# Convenience wrapper for running the Discord Radar blocker observer from profile-scoped scripts/
+# or directly via cron no-agent mode.
+set -euo pipefail
+
+: "${HERMES_HOME:=/home/hermes/.hermes}"
+: "${PROFILE:=otto}"
+
+STATE_PATH="${HERMES_HOME}/profiles/${PROFILE}/cron/discord-now-state.json"
+
+if [ -x "/home/hermes/.hermes/hermes-agent/venv/bin/python" ]; then
+    PYTHON_BIN="/home/hermes/.hermes/hermes-agent/venv/bin/python"
+else
+    PYTHON_BIN="$(command -v python3 || command -v python)"
+fi
+
+exec "$PYTHON_BIN" -m hermes_cli.main radar-observer \
+    --state-path "$STATE_PATH" \
+    --profile "$PROFILE" "$@"
+