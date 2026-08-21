#!/bin/bash
# Export Peloton workout IDs keyed by the CSV export's workout timestamp
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=/dev/null
source ~/.openclaw/.env 2>/dev/null
export OP_SERVICE_ACCOUNT_TOKEN
exec uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/peloton_workout_ids.py" "$@"
