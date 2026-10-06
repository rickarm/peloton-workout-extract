# peloton-workout-extract

Extract structured metadata and Power Zone breakdowns from Peloton workout detail pages. Outputs JSON designed to flow into the Peloton-Rides Airtable table.

## Setup

```bash
cd ~/Dev/peloton-workout-extract
uv sync
uv run playwright install chromium
```

### Credentials

The scripts read `PELOTON_EMAIL` and `PELOTON_PASSWORD` from the process
environment. Nothing reads or sources a local secrets file; the caller injects
them, normally from a 1Password Environment:

```bash
op run --environment "$OP_ENVIRONMENT_ID" -- ./peloton-csv-download.sh
```

`op run --environment` needs a 1Password CLI build with Environments support
(2.33.0-beta.02 or later; the stable 2.35.0 does not have it). The caller also
provides `OP_SERVICE_ACCOUNT_TOKEN`; it never comes from a file. Missing
credentials exit with status 2 and a message naming the missing variable,
without printing any value.

Optional fallback: set `PELOTON_OP_VAULT` (and optionally `PELOTON_OP_ITEM`,
default `www.onepeloton.com`) to have `auth.py` read the `username` and
`password` fields with `op read` instead. `OP_CLI` overrides which `op` binary
is used. Prefer the environment path; nothing is tied to a particular vault.

Credentials are only needed when the browser has to log in. The session is
cached at `~/.cache/peloton-skill/storage_state.json` (chmod 600; move it with
`PELOTON_CACHE_DIR`). **That file holds a live Peloton bearer token**, which
the API tools reuse until it expires (48h), so treat it as a secret.

First run — capture a login session:

```bash
op run --environment "$OP_ENVIRONMENT_ID" -- uv run python peloton_extract.py <WORKOUT_URL> --headed
```

## Usage

Run every command below under `op run --environment "$OP_ENVIRONMENT_ID" --`
(see Credentials), or with `PELOTON_EMAIL`/`PELOTON_PASSWORD` already exported.

```bash
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

## Class metadata (`peloton_class_resolve.py`)

Resolves what a class *was* — title, instructor, duration, and its planned
power-zone breakdown — straight from the API, by class ID or by workout ID.

Works on classes you have **never taken**, so you can preview a ride's zone plan
before committing to it.

```bash
# By class ID
./peloton-class-resolve.sh --class-id f3105dfcd6a0445eab35d50c5b207c80

# By class URL — extra params like categorySlug and the account-tied `code=`
# share token are ignored; only `classId` matters
./peloton-class-resolve.sh --class-id "https://members.onepeloton.com/classes/cycling?modal=classDetailsModal&classId=f3105dfcd6a0445eab35d50c5b207c80"

# By workout ID or workout URL — the class link comes from `ride.id`,
# so it is a fact, not a guess
./peloton-class-resolve.sh --workout-id c9b9d832b80a4352a63c5c80df5aa0e9

# Many at once, as a CSV ready to join against Airtable
./peloton-class-resolve.sh --stdin --format csv < class-ids.txt
```

Passing a workout URL to `--class-id` (or the reverse) is an error naming the
right flag, rather than a silent lookup of the wrong thing.

Each record carries `class_timestamp` in the `YYYY-MM-DD HH:mm (ZZ)` shape the
`Peloton-Rides` table keys on, plus the plan in two forms:

- `zones` — zone number to total planned seconds. CSV flattens this to
  `zone1_sec` ... `zone7_sec`.
- `segments` — the ordered plan, `{zone, start_sec, end_sec, duration_sec}` per
  block. A repeated zone stays as separate entries here, because three separate
  minutes in zone 5 is a different ride from one three-minute block. CSV reports
  only `segment_count`; use `json`/`jsonl` for the sequence.

This supersedes scraping the class page for metadata, and supersedes matching a
workout to a class by score. Three things are worth knowing:

- **Zone offsets are inclusive**, so a 60..359 segment is 300 seconds. The
  totals reconcile exactly with what the Playwright scrape used to store.
- **Classes are keyed on `scheduled_start_time`**, not `original_air_time` —
  the latter is when the stream actually rolled, several minutes before the
  published slot, and would mint a near-duplicate row for every class.
- A workout with no class (freestyle, Apple Health) returns
  `{"workout_id": ..., "class_id": null}` rather than being dropped.

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
| `peloton_class_resolve.py` | CLI entrypoint — class metadata and planned power zones |
| `peloton_api.py` | Peloton REST client (token extraction, paging, merge-key formatting) |
| `pel_selectors.py` | Playwright selector inventory (update here when Peloton changes UI) |
| `auth.py` | Credentials from the environment (optional `op read` fallback) + session persistence |
| `tests/` | Unit tests for the pure logic — no network, no browser |

## Notes

- Non-cycling workouts produce a minimal record (no class plan).
- The `notes` field is empty — class descriptions aren't exposed in the workout page DOM.
- Peloton's class plan is an accordion (one segment open at a time), so the script expands and extracts each segment sequentially.
