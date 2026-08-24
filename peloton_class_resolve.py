#!/usr/bin/env python3
"""Resolve Peloton class metadata from the API, by class id or by workout id.

`Peloton-Rides` rows were previously filled by scraping the class page with
Playwright, and workouts were attached to them by `Peloton_Match.py`, which
*scores* title, instructor, duration and start time against a threshold. The
API returns the same metadata authoritatively and returns the workout-to-class
link as a fact, so neither the scrape nor the score is needed for any workout
whose id is known.

Emits one record per resolved class. A workout with no class (freestyle, Apple
Health) resolves to a record with `class_id: null` rather than being dropped,
so a caller can tell "no class" apart from "not looked up".

Usage:
    peloton_class_resolve --class-id ID [--class-id ID ...]
    peloton_class_resolve --workout-id ID [--workout-id ID ...]
    peloton_class_resolve --stdin < ids.txt
        [--format json|jsonl|csv] [--output-file PATH] [--timezone ZONE]
        [--refresh-session] [--headed]
"""

import argparse
import csv
import io
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from peloton_api import (
    DEFAULT_CLASS_TZ,
    PelotonAPI,
    PelotonAPIError,
)
from peloton_workout_ids import get_token

VERSION = "1.1.0"

# Peloton ids are 32 lowercase hex characters.
ID_RE = re.compile(r"^[0-9a-f]{32}$")
CLASS_URL_MARKER = "/classes/"
WORKOUT_URL_MARKER = "/profile/workouts/"

# Peloton's power zones are 1-7; the table has a column per zone.
ZONE_NUMBERS = range(1, 8)

CSV_COLUMNS = [
    "workout_id",
    "class_id",
    "title",
    "instructor",
    "duration_min",
    "fitness_discipline",
    "class_timestamp",
    "is_power_zone_class",
    "is_ftp_test",
    "class_types",
    "class_url",
    "segment_count",
] + [f"zone{n}_sec" for n in ZONE_NUMBERS]


def _bare_id(text: str) -> str | None:
    candidate = text.strip()
    return candidate if ID_RE.match(candidate) else None


def class_id_from_text(text: str) -> str:
    """Accept a class URL or a bare class id and return the id.

    Class URLs carry the id in a `classId` query parameter, alongside params
    that must be ignored: `categorySlug` is cosmetic and `code` is a share
    token tied to the account that generated it, so requiring it would make a
    link unusable by anyone else.

    A workout URL is rejected rather than accepted, because its id is a
    *workout* id — resolving it as a class id would quietly look up the wrong
    thing, or nothing at all.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("empty class id")

    bare = _bare_id(text)
    if bare:
        return bare

    if WORKOUT_URL_MARKER in text:
        raise ValueError(
            f"that is a workout URL, not a class URL: {text}. "
            "Pass it with --workout-id instead."
        )

    query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    for value in query.get("classId", []):
        found = _bare_id(value)
        if found:
            return found

    raise ValueError(f"no class id found in {text!r}")


def workout_id_from_text(text: str) -> str:
    """Accept a workout URL or a bare workout id and return the id."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty workout id")

    bare = _bare_id(text)
    if bare:
        return bare

    if CLASS_URL_MARKER in text or "classId=" in text:
        raise ValueError(
            f"that is a class URL, not a workout URL: {text}. "
            "Pass it with --class-id instead."
        )

    path = urllib.parse.urlparse(text).path
    if WORKOUT_URL_MARKER in path:
        found = _bare_id(path.rsplit("/", 1)[-1])
        if found:
            return found

    raise ValueError(f"no workout id found in {text!r}")


def read_stdin_ids() -> list[str]:
    return [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]


def resolve_all(
    api: PelotonAPI,
    class_ids: list[str],
    workout_ids: list[str],
    tz_name: str,
) -> list[dict]:
    """Resolve every requested id, keeping request order and de-duplicating.

    A class that several workouts share is fetched once. Each workout still
    gets its own output record so the caller can join on `workout_id`.
    """
    cache: dict[str, dict] = {}

    def resolve(class_id: str) -> dict:
        if class_id not in cache:
            cache[class_id] = api.resolve_class(class_id, tz_name=tz_name)
        return dict(cache[class_id])

    records = []
    for class_id in class_ids:
        record = resolve(class_id)
        record["workout_id"] = None
        records.append(record)
    for workout_id in workout_ids:
        class_id = api.class_id_for_workout(workout_id)
        if class_id is None:
            records.append({"workout_id": workout_id, "class_id": None})
            continue
        record = resolve(class_id)
        record["workout_id"] = workout_id
        records.append(record)
    return records


def flatten_for_csv(record: dict) -> dict:
    """One CSV row per class.

    The segment plan is a list, which a CSV cell cannot carry usefully, so the
    row reports its length and callers who need the sequence use json/jsonl.
    """
    zones = record.get("zones") or {}
    row = {k: record.get(k) for k in CSV_COLUMNS}
    row["class_types"] = "|".join(record.get("class_types") or [])
    row["segment_count"] = len(record.get("segments") or [])
    for n in ZONE_NUMBERS:
        row[f"zone{n}_sec"] = zones.get(n)
    return row


def render(records: list[dict], fmt: str) -> str:
    if fmt == "jsonl":
        return "\n".join(json.dumps(r) for r in records)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for record in records:
            writer.writerow(flatten_for_csv(record))
        return buf.getvalue().rstrip("\n")
    return json.dumps(
        {
            "classes": records,
            "run_metadata": {
                "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tool_version": VERSION,
                "total_classes": len(records),
            },
        },
        indent=2,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve Peloton class metadata by class id or workout id"
    )
    parser.add_argument(
        "--class-id", action="append", default=[], metavar="ID_OR_URL",
        help="Class id, or a class URL to read it from (repeatable)",
    )
    parser.add_argument(
        "--workout-id", action="append", default=[], metavar="ID_OR_URL",
        help="Workout id, or a workout URL, whose class should be resolved (repeatable)",
    )
    parser.add_argument(
        "--stdin", action="store_true",
        help="Also read ids from stdin, one per line (treated as --class-id "
             "unless --stdin-is-workouts is given)",
    )
    parser.add_argument(
        "--stdin-is-workouts", action="store_true",
        help="Treat ids read from stdin as workout ids",
    )
    parser.add_argument(
        "--timezone", default=DEFAULT_CLASS_TZ,
        help=f"Zone for the class_timestamp field (default: {DEFAULT_CLASS_TZ})",
    )
    parser.add_argument(
        "--format", choices=["json", "jsonl", "csv"], default="json", help="Output format"
    )
    parser.add_argument("--output-file", help="Write output here instead of stdout")
    parser.add_argument(
        "--refresh-session", action="store_true",
        help="Re-run the browser login even if the cached token still looks valid",
    )
    parser.add_argument("--headed", action="store_true", help="Run the browser visibly")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    raw_classes = list(args.class_id)
    raw_workouts = list(args.workout_id)
    if args.stdin:
        piped = read_stdin_ids()
        if args.stdin_is_workouts:
            raw_workouts += piped
        else:
            raw_classes += piped

    if not raw_classes and not raw_workouts:
        parser.error("give at least one --class-id, --workout-id, or --stdin")

    # Parse every input up front: a typo in the tenth URL should fail before
    # the first nine have been fetched.
    try:
        class_ids = [class_id_from_text(v) for v in raw_classes]
        workout_ids = [workout_id_from_text(v) for v in raw_workouts]
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        api = PelotonAPI(get_token(args.refresh_session, args.headed))
        records = resolve_all(api, class_ids, workout_ids, args.timezone)
    except PelotonAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output = render(records, args.format)
    if args.output_file:
        path = Path(args.output_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n")
        print(f"Wrote {len(records)} classes to {path}", file=sys.stderr)
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
