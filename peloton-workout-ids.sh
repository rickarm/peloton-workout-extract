#!/bin/bash
# Export Peloton workout IDs keyed by the CSV export's workout timestamp
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/peloton_workout_ids.py" "$@"
