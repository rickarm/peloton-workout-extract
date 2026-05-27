# peloton-workout-extract

Extract structured metadata + Power Zone breakdowns from Peloton workout detail pages.

## Quick reference

- **Run**: `cd ~/Dev/peloton-workout-extract && source ~/.openclaw/.env && export OP_SERVICE_ACCOUNT_TOKEN && uv run python peloton_extract.py <URL> [--dry-run] [--headed] [--format jsonl]`
- **1Password item**: `op://Vault-agent-mandy/www.onepeloton.com/{username,password}` (no 2FA)
- **Session cache**: `~/.cache/peloton-skill/storage_state.json` (chmod 600)
- **Debug logs**: `~/.cache/peloton-skill/logs/`
- **Airtable target** (downstream, not this tool): base `appBmQA2p3z2Fdofa`, table `tblht11eg2nJ5gh3o` (Peloton-Rides)

## Architecture

| File | Purpose |
|------|---------|
| `peloton_extract.py` | CLI entrypoint — validates URLs, orchestrates browser, emits JSON |
| `pel_selectors.py` | Single dict of Playwright selectors (update here when Peloton changes UI) |
| `auth.py` | 1Password credential fetch + Playwright session persistence |

## Key behaviors

- Peloton uses `data-test-id` (hyphenated), not `data-testid`
- Class plan is an accordion — only one segment expands at a time. Extract subsegments immediately after expanding each segment.
- `notes` field is empty — class description isn't in the workout page DOM (would need Peloton API)
- Renamed `selectors.py` → `pel_selectors.py` to avoid shadowing Python's stdlib `selectors` module

## Gotchas

- Cookie banner appears on first visit even with saved session — script dismisses it automatically
- `OP_SERVICE_ACCOUNT_TOKEN` must be exported before running (source `~/.openclaw/.env`)
- `class_timestamp` must be formatted as `YYYY-MM-DD HH:mm (ZZ)` (e.g. `2026-04-17 07:00 (-07)`) to match the Peloton-Rides Airtable table. `format_class_timestamp()` handles this.
- Peloton timestamps without timezone (e.g. `Mon 11/24/25 @ 6:30 AM`) default to ET (EST/EDT by month). `AM`/`PM` must be excluded from timezone regex matching.
- Airtable sync workflow is defined in the skill file (`~/.claude/skills/peloton-extract/SKILL.md`), not here — that's the source of truth for field mappings, duplicate checks, and instructor lookup.
