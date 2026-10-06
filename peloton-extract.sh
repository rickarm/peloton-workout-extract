#!/bin/bash
# Extract structured metadata from Peloton workout URLs
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/peloton_extract.py" "$@"
