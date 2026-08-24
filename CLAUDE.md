# peloton-workout-extract

Extract structured metadata + Power Zone breakdowns from Peloton workout detail pages.

## Quick reference

- **Run extract**: `~/Dev/peloton-workout-extract/peloton-extract.sh <URL> [--dry-run] [--headed] [--format jsonl]`
- **Run CSV download**: `~/Dev/peloton-workout-extract/peloton-csv-download.sh [--headed] [--output-dir /tmp]`
- **Run workout-ID export**: `~/Dev/peloton-workout-extract/peloton-workout-ids.sh [--all|--limit N] [--since YYYY-MM-DD] [--format json|jsonl|csv] [--output-file PATH]`
- **Run class resolve**: `~/Dev/peloton-workout-extract/peloton-class-resolve.sh [--class-id ID_OR_URL ...] [--workout-id ID_OR_URL ...] [--stdin] [--format json|jsonl|csv] [--timezone ZONE]`
- **1Password item**: `op://Vault-agent-mandy/www.onepeloton.com/{username,password}` (no 2FA)
- **Session cache**: `~/.cache/peloton-skill/storage_state.json` (chmod 600)
- **Debug logs**: `~/.cache/peloton-skill/logs/`
- **Airtable target** (downstream, not this tool): base `appBmQA2p3z2Fdofa`, table `tblht11eg2nJ5gh3o` (Peloton-Rides)

## Architecture

| File | Purpose |
|------|---------|
| `peloton_extract.py` | CLI entrypoint — validates URLs, orchestrates browser, emits JSON |
| `peloton_csv_download.py` | CLI entrypoint for CSV export via headless Playwright |
| `peloton_workout_ids.py` | CLI entrypoint — workout IDs from the Peloton API, keyed by the CSV merge key |
| `peloton_class_resolve.py` | CLI entrypoint — class metadata + planned power zones, by class ID or workout ID |
| `peloton_api.py` | Peloton REST client — token extraction, paging, timestamp formatting |
| `pel_selectors.py` | Single dict of Playwright selectors (update here when Peloton changes UI) |
| `auth.py` | 1Password credential fetch + Playwright session persistence |
| `peloton-extract.sh` | Shell wrapper for extract (sources 1Password token, invokes uv) |
| `peloton-csv-download.sh` | Shell wrapper for CSV download (sources 1Password token, invokes uv) |
| `peloton-workout-ids.sh` | Shell wrapper for workout-ID export (sources 1Password token, invokes uv) |
| `peloton-class-resolve.sh` | Shell wrapper for class resolve (sources 1Password token, invokes uv) |

## Key behaviors

- Peloton uses `data-test-id` (hyphenated), not `data-testid`
- Class plan is an accordion — only one segment expands at a time. Extract subsegments immediately after expanding each segment.
- `notes` field is empty — class description isn't in the workout page DOM (would need Peloton API)
- Renamed `selectors.py` → `pel_selectors.py` to avoid shadowing Python's stdlib `selectors` module

## Peloton API

- `POST /auth/login` is **dead** — 403 "Endpoint no longer accepting requests". The only working credential is the Auth0 access token in the members app's `localStorage`, which Playwright already persists into `storage_state.json`. `peloton_api.extract_access_token()` reads it out of the `@@auth0spajs@@::...::https://api.onepeloton.com/::...` entry; the sibling `@@user@@` entry holds an `id_token` and is NOT usable.
- Token TTL is 48h (`expiresAt` in that same entry). `peloton_workout_ids.py` reuses a fresh one and only launches a browser when it has to, so repeat runs cost ~1s instead of ~9s.
- Required headers: `Authorization: Bearer <token>` plus `Peloton-Platform: web`.
- Endpoints used: `GET /api/me` (user id) and `GET /api/user/<uid>/workouts?limit=100&page=N&joins=ride&sort_by=-created`. `limit` caps at 100; a full account (~2,800 workouts) is 29 pages, about 60s.
- **`sort_by=-created` is creation order, not start order.** A workout imported after the fact (Apple Health, a late device sync) can carry an older `start_time` than the record before it — 4 such inversions in 2,811 workouts, up to 12 min. Anything that stops paging early must do so on a whole page, never on the first out-of-range record.
- The CSV's `Workout Timestamp` equals `start_time` rendered in that workout's **own** `timezone` (Peloton emits POSIX-style `Etc/GMT+7` = UTC-7 alongside IANA names). That equality is what makes the join work — verified against 2,743 rows.
- Freestyle / Apple Health workouts carry `ride.id` = 32 zeros, not a missing join. `normalize_workout()` maps that to null so they don't all link to one phantom class.
- The API returns more workouts than the CSV export does (2,811 vs 2,743) — the export drops most `cardio` and Apple-Health-imported activity.

## Class metadata from the API

`peloton_class_resolve.py` replaces the Playwright class scrape and the fuzzy
score-matching for any workout whose ID is known. The class link is a fact
(`ride.id`), not a score.

- `GET /api/ride/<id>/details` is the workhorse: it embeds the ride, the
  instructor, and `target_metrics_data`, so a class costs **one** request.
  `/api/ride/<id>` + `/api/instructor/<id>` is only the fallback when that
  payload has no embedded instructor. Instructor names are cached per client,
  misses included.
- **Zone segment offsets are inclusive on both ends** — a segment running
  60..359 is 300 seconds, not 299. `sum_target_zones()` handles this; the
  totals reconcile exactly against the values the old scraper stored.
- **Key classes on `scheduled_start_time`, not `original_air_time`.** The air
  time is when the stream actually rolled — a few minutes early, with seconds
  (`2026-01-02 22:25:05Z` against a `22:30:00Z` slot). `Peloton-Rides` keys on
  the scheduled slot, so using the air time mints a near-duplicate row for
  every class already in the table. `class_air_time()` picks the right one.
- Existing `ClassTimestamp` values in Airtable carry **mislabelled offsets**
  (`2026-01-02 14:30 (-05)` is a Pacific wall clock with an Eastern label —
  the scraper's ET-default bug). The wall-clock time matches what this tool
  emits; the offset does not. Match on the time, not the label.
- Accepts **class URLs and workout URLs** as well as bare ids. Only `classId`
  is read from a class URL; `categorySlug` and the account-tied `code=` share
  token are ignored. A workout URL passed to `--class-id` is an error naming
  the right flag, never a silent lookup of the wrong id.
- **Works on classes never taken**, so a zone plan can be previewed before
  riding. That is what closed the old class-scrape request (#4).
- The plan comes back two ways: `zones` (per-zone totals) and `segments` (the
  ordered blocks). Repeated zones stay separate in `segments` and are only
  collapsed in `zones`. CSV carries `segment_count` only.
- Zone totals sum to slightly **less** than class duration — warmup and
  cooldown carry no `power_zone` metric. Expected, not a parsing bug.
- A workout with no class resolves to `{"workout_id": ..., "class_id": null}`
  rather than being dropped, so "no class" stays distinguishable from "not
  looked up".
- `PowerZoneType` is **not** derivable from the class title — a generic
  "Power Zone Ride" splits across PZ Max and Threshold. Classifying it needs
  the planned zone distribution. Nothing here writes that field.

## Gotchas

- Cookie banner appears on first visit even with saved session — script dismisses it automatically
- `OP_SERVICE_ACCOUNT_TOKEN` must be exported before running — the shell wrappers handle this automatically
- `class_timestamp` must be formatted as `YYYY-MM-DD HH:mm (ZZ)` (e.g. `2026-04-17 07:00 (-07)`) to match the Peloton-Rides Airtable table. `format_class_timestamp()` handles this.
- Peloton timestamps without timezone (e.g. `Mon 11/24/25 @ 6:30 AM`) default to ET (EST/EDT by month). `AM`/`PM` must be excluded from timezone regex matching.
- Airtable sync workflow is defined in the skill file (`~/.claude/skills/peloton-extract/SKILL.md`), not here — that's the source of truth for field mappings, duplicate checks, and instructor lookup.
