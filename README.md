# peloton-workout-extract

Extract structured metadata and Power Zone breakdowns from Peloton workout detail pages. Outputs JSON designed to flow into the Peloton-Rides Airtable table.

## Setup

```bash
cd ~/Dev/peloton-workout-extract
uv sync
uv run playwright install chromium
```

Requires:
- 1Password CLI (`op`) with access to `Vault-agent-mandy`
- Peloton credentials stored as `www.onepeloton.com` in that vault

First run — capture a login session:

```bash
source ~/.openclaw/.env && export OP_SERVICE_ACCOUNT_TOKEN
uv run python peloton_extract.py <WORKOUT_URL> --headed
```

Subsequent runs reuse the saved session at `~/.cache/peloton-skill/storage_state.json`.

## Usage

```bash
source ~/.openclaw/.env && export OP_SERVICE_ACCOUNT_TOKEN

# Single workout
uv run python peloton_extract.py https://members.onepeloton.com/profile/workouts/<id>

# Multiple workouts
uv run python peloton_extract.py <url1> <url2> <url3>

# Pipe URLs from stdin
cat urls.txt | uv run python peloton_extract.py

# Save to file
uv run python peloton_extract.py <url> --output-file output.json

# JSONL format (one record per line)
uv run python peloton_extract.py <url> --format jsonl

# Debug: dump raw HTML + screenshot without parsing
uv run python peloton_extract.py <url> --dry-run
```

## Output

```json
{
  "workouts": [
    {
      "discipline": "cycling",
      "workout_timestamp": "Tue 5/12/26 @ 5:16 PM",
      "ride_title": "75 min Power Zone Endurance Ride",
      "instructor": "Matt Wilpers",
      "duration_minutes": 75,
      "class_id": "33d4b401875e4fc5bcadb34fac42b755",
      "class_plan_zones": {
        "zone_1": "8:06",
        "zone_2": "21:00",
        "zone_3": "43:00",
        "zone_4": "0:00",
        "zone_5": "0:00",
        "zone_6": "0:00",
        "zone_7": "0:00",
        "unassigned": "2:54"
      },
      "class_plan_breakdown": [
        {
          "segment": "Warm Up",
          "duration": "12 min",
          "movements": [
            { "name": "Zone 1", "duration": "5:02" },
            { "name": "Spin Ups", "duration": "2:54" }
          ]
        }
      ],
      "extraction_status": "ok"
    }
  ],
  "run_metadata": {
    "extracted_at": "2026-05-15T02:08:39Z",
    "skill_version": "1.0.0",
    "total_requested": 1,
    "total_succeeded": 1,
    "total_failed": 0
  }
}
```

## Project structure

| File | Purpose |
|------|---------|
| `peloton_extract.py` | CLI entrypoint |
| `pel_selectors.py` | Playwright selector inventory (update here when Peloton changes UI) |
| `auth.py` | 1Password credential fetch + session persistence |

## Notes

- Non-cycling workouts produce a minimal record (no class plan).
- The `notes` field is empty — class descriptions aren't exposed in the workout page DOM.
- Peloton's class plan is an accordion (one segment open at a time), so the script expands and extracts each segment sequentially.
