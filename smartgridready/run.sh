#!/bin/sh
# SmartGridready add-on launcher.
# Reads add-on options from /data/options.json and starts the Python service.

set -e

OPTIONS_FILE="/data/options.json"
if [ -f "${OPTIONS_FILE}" ]; then
    LOG_LEVEL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("log_level","info"))' "${OPTIONS_FILE}" 2>/dev/null || echo info)"
else
    LOG_LEVEL="info"
fi

export PYTHONPATH="/opt/smartgridready"
export SGR_LOG_LEVEL="${LOG_LEVEL}"
export SGR_OPTIONS_FILE="${OPTIONS_FILE}"

if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    echo "[smartgridready] WARNING: SUPERVISOR_TOKEN not set — running without HA access"
fi

echo "[smartgridready] starting (log_level=${LOG_LEVEL})"
exec python3 -m src.main
