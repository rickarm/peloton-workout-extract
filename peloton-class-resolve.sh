#!/bin/bash
# Resolve Peloton class metadata from the API, by class id or workout id
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec uv run --project "$SCRIPT_DIR" python "$SCRIPT_DIR/peloton_class_resolve.py" "$@"
