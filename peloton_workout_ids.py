#!/usr/bin/env python3
"""Export Peloton workout IDs keyed by the CSV export's workout timestamp.

The Peloton CSV export has no workout ID column, so an Airtable row synced from
it can't be linked back to `members.onepeloton.com/profile/workouts/<id>`. This
pulls the IDs from the Peloton API and emits them alongside a
`workout_timestamp` in the exact shape the CSV uses, so the downstream sync can
join the two on its existing merge key.

Usage:
    peloton_workout_ids [--limit N | --all] [--since YYYY-MM-DD]
                        [--format json|jsonl|csv] [--output-file PATH]
                        [--refresh-session] [--headed]
"""

import argparse
import csv
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from auth import (
    STORAGE_STATE_PATH,
    dismiss_cookie_banner,
    do_login,
    has_storage_state,
    is_logged_in,
    save_storage_state,
)
from peloton_api import (
    MAX_PAGE_SIZE,
    PelotonAPI,
    PelotonAPIError,
    normalize_workout,
    read_cached_token,
    token_is_fresh,
)

VERSION = "1.0.0"
WORKOUTS_URL = "https://members.onepeloton.com/profile/workouts"
DEFAULT_LIMIT = 100

CSV_COLUMNS = [
    "workout_id",
    "workout_timestamp",
    "start_time_utc",
    "timezone",
    "fitness_discipline",
    "workout_type",
    "status",
    "device_type",
    "class_id",
    "class_title",
]


def parse_since(value: str) -> int:
    """Parse a --since date into a UTC epoch cutoff."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"--since must be YYYY-MM-DD, got {value!r}"
        ) from None
    return int(day.replace(tzinfo=timezone.utc).timestamp())


def refresh_session(headed: bool = False) -> None:
    """Drive a browser through the members site so Auth0 mints a fresh token.

    Imported lazily: a cached-token run should not pay Playwright's import or
    require a browser to be installed.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context_kwargs = {"viewport": {"width": 1280, "height": 900}}
        if has_storage_state():
            context_kwargs["storage_state"] = STORAGE_STATE_PATH
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        try:
            page.goto(WORKOUTS_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)
            dismiss_cookie_banner(page)
            if not is_logged_in(page):
                print("Not logged in - running auth flow...", file=sys.stderr)
                do_login(page)
                page.goto(WORKOUTS_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
                dismiss_cookie_banner(page)
            # The web app swaps its Auth0 token into localStorage shortly after
            # the page boots; give it a beat before snapshotting.
            time.sleep(3)
            save_storage_state(context)
        finally:
            browser.close()


def get_token(refresh: bool, headed: bool) -> str:
    token, expires_at = read_cached_token(STORAGE_STATE_PATH)
    if refresh or not token or not token_is_fresh(expires_at):
        reason = "forced" if refresh else "missing or expiring"
        print(f"Refreshing Peloton session ({reason})...", file=sys.stderr)
        refresh_session(headed=headed)
        token, expires_at = read_cached_token(STORAGE_STATE_PATH)
    if not token:
        raise PelotonAPIError(
            "No Peloton API token in the saved session. Run once with "
            "--refresh-session --headed to complete a login."
        )
    return token


def collect_workouts(
    api: PelotonAPI,
    user_id: str,
    limit: int | None,
    since_epoch: int | None,
) -> list[dict]:
    """Walk the API newest-first, stopping as soon as the bounds are satisfied.

    `--since` stops only once an entire page falls below the cutoff. Peloton
    sorts by creation time, so a late-imported workout can sit next to one that
    started later; stopping at the first old record would drop the ones behind
    it.
    """
    page_size = MAX_PAGE_SIZE if limit is None else min(limit, MAX_PAGE_SIZE)
    records: list[dict] = []
    for page in api.iter_workout_pages(user_id, page_size=page_size):
        kept_any = False
        for raw in page:
            start_time = raw.get("start_time")
            if since_epoch is not None and (start_time is None or start_time < since_epoch):
                continue
            kept_any = True
            records.append(normalize_workout(raw))
            if limit is not None and len(records) >= limit:
                return sort_newest_first(records)
        if since_epoch is not None and not kept_any:
            break
    return sort_newest_first(records)


def sort_newest_first(records: list[dict]) -> list[dict]:
    """Order by workout start, newest first — Peloton's own order is by creation."""
    return sorted(records, key=lambda r: r.get("start_time_utc") or "", reverse=True)


def render(records: list[dict], fmt: str) -> str:
    if fmt == "jsonl":
        return "\n".join(json.dumps(r) for r in records)
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for record in records:
            writer.writerow({k: record.get(k) for k in CSV_COLUMNS})
        return buf.getvalue().rstrip("\n")
    return json.dumps(
        {
            "workouts": records,
            "run_metadata": {
                "extracted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tool_version": VERSION,
                "total_workouts": len(records),
            },
        },
        indent=2,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Peloton workout IDs keyed by CSV workout timestamp"
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Most recent N workouts (default: {DEFAULT_LIMIT})",
    )
    scope.add_argument(
        "--all",
        action="store_true",
        help="Every workout on the account (use for a one-off backfill)",
    )
    parser.add_argument(
        "--since",
        type=parse_since,
        help="Only workouts starting on or after this date (YYYY-MM-DD, UTC)",
    )
    parser.add_argument(
        "--format", choices=["json", "jsonl", "csv"], default="json", help="Output format"
    )
    parser.add_argument("--output-file", help="Write output here instead of stdout")
    parser.add_argument(
        "--refresh-session",
        action="store_true",
        help="Re-run the browser login even if the cached token still looks valid",
    )
    parser.add_argument("--headed", action="store_true", help="Run the browser visibly")
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1 and not args.all:
        parser.error("--limit must be at least 1")

    limit = None if args.all else args.limit

    try:
        token = get_token(args.refresh_session, args.headed)
        api = PelotonAPI(token)
        user_id = api.user_id()
        records = collect_workouts(api, user_id, limit, args.since)
    except PelotonAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output = render(records, args.format)
    if args.output_file:
        path = Path(args.output_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + "\n")
        print(f"Wrote {len(records)} workouts to {path}", file=sys.stderr)
    else:
        print(output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
