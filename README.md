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

## Workout IDs (`peloton_workout_ids.py`)

The Peloton CSV export has no workout ID column, so an Airtable row synced from
it can't be linked back to `members.onepeloton.com/profile/workouts/<id>`. This
tool pulls the IDs from the Peloton API and emits them alongside a
`workout_timestamp` in the exact shape the CSV uses, so the downstream sync can
join the two on its existing merge key.

```bash
source ~/.openclaw/.env && export OP_SERVICE_ACCOUNT_TOKEN

# 100 most recent workouts (default)
./peloton-workout-ids.sh

# Everything on the account, as CSV — the one-off backfill
./peloton-workout-ids.sh --all --format csv --output-file /tmp/workout-ids.csv

# Only what the last sync could have missed
./peloton-workout-ids.sh --all --since 2026-08-01 --format csv

# Force a new browser login (the cached token is reused until it expires)
./peloton-workout-ids.sh --refresh-session --headed
```

| Column | Notes |
|--------|-------|
| `workout_id` | 32-char hex — the `<id>` in the workout URL |
| `workout_timestamp` | `YYYY-MM-DD HH:MM` in the workout's own timezone; the CSV merge key |
| `start_time_utc` | ISO-8601 UTC, for unambiguous ordering |
| `timezone` | IANA name Peloton recorded, e.g. `America/New_York`, `Etc/GMT+7` |
| `fitness_discipline`, `workout_type`, `status`, `device_type` | Straight from the API |
| `class_id` | `ride.id` — the Peloton-Rides key. Null for freestyle/Apple Health workouts |
| `class_title` | Useful as a tiebreaker (see below) |

### Joining to the CSV

Normalize the CSV's `Workout Timestamp` the way the Airtable sync already does
(strip the trailing `(PDT)` / `(-07)`) and match it against `workout_timestamp`.
Measured against a full export of 2,743 rows on 2026-08-21:

- 2,729 rows (99.5%) matched exactly one workout ID
- 8 more resolved once `class_title` broke a same-minute tie
- 6 rows stayed ambiguous — three pairs of workouts that started in the same
  minute *and* were the same class (2020-04-24, 2022-03-22 ×2)
- 0 rows failed to match

The API also returns workouts the CSV export omits — 68 of them, mostly
`cardio` and Apple Health imports — so expect more API records than CSV rows.

### Auth

Peloton retired `POST /auth/login` (it now answers 403 "Endpoint no longer
accepting requests"), so the API is reachable only with the Auth0 access token
the members web app keeps in `localStorage`. That store is part of the
Playwright session `auth.py` already persists, so the saved browser session
doubles as an API credential. The token lasts 48h; the tool reuses it while
it's fresh and silently drives a headless browser to mint a new one when it
isn't.

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
| `peloton_extract.py` | CLI entrypoint — workout page scraping |
| `peloton_csv_download.py` | CLI entrypoint — workout CSV export |
| `peloton_workout_ids.py` | CLI entrypoint — workout IDs from the Peloton API |
| `peloton_api.py` | Peloton REST client (token extraction, paging, merge-key formatting) |
| `pel_selectors.py` | Playwright selector inventory (update here when Peloton changes UI) |
| `auth.py` | 1Password credential fetch + session persistence |
| `tests/` | Unit tests for the pure logic — no network, no browser |

## Notes

- Non-cycling workouts produce a minimal record (no class plan).
- The `notes` field is empty — class descriptions aren't exposed in the workout page DOM.
- Peloton's class plan is an accordion (one segment open at a time), so the script expands and extracts each segment sequentially.
