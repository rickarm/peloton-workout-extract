#!/bin/bash
# Download Peloton workout CSV via headless Playwright
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/peloton_csv_download.py" "$@"
