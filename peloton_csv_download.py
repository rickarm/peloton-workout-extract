#!/usr/bin/env python3
"""Download the Peloton workout CSV via headless browser.

Usage:
    peloton_csv_download [--headed] [--output-dir DIR] [--timeout SECONDS]

The CSV goes to PELOTON_CSV_DIR (default ~/.local/share/peloton-sync/csv; see
csv_dir.py). If no CSV lands within the timeout, it exits 3 with a message
instead of hanging.
"""

import argparse
import os
import sys
import threading
import time

from playwright.sync_api import sync_playwright

from auth import (
    STORAGE_STATE_PATH,
    dismiss_cookie_banner,
    do_login,
    has_storage_state,
    is_logged_in,
    save_storage_state,
)

from csv_dir import ENV_KEY, CsvDirError, ensure_csv_dir, resolve_csv_dir

WORKOUTS_URL = "https://members.onepeloton.com/profile/workouts"
TIMEOUT_DEFAULT = int(os.environ.get("PELOTON_CSV_TIMEOUT", "120"))
EXIT_TIMEOUT = 3


def start_watchdog(seconds, output_dir):
    """Exit with a clear message if the CSV hasn't landed in `seconds`.

    A blocked file write (e.g. macOS per-app folder protection) stalls inside
    the browser driver with no error, so a Playwright timeout never fires.
    os._exit is deliberate: a normal exit would wait on the stalled driver.
    """

    def fire():
        print(
            f"ERROR: no Peloton CSV landed in {output_dir} within {seconds}s. "
            f"The download or the file write is stuck. If that directory is in a "
            f"macOS-protected folder, set {ENV_KEY} to one outside it.",
            file=sys.stderr,
            flush=True,
        )
        os._exit(EXIT_TIMEOUT)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()
    return timer


def main():
    parser = argparse.ArgumentParser(description="Download Peloton workout CSV")
    parser.add_argument("--headed", action="store_true", help="Run browser visibly")
    parser.add_argument(
        "--output-dir",
        default=None,
        help=f"Directory for the CSV (default: ${ENV_KEY}, else ~/.local/share/peloton-sync/csv)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=TIMEOUT_DEFAULT,
        help=f"Seconds from the download click until the CSV must be on disk "
        f"(default: {TIMEOUT_DEFAULT}, or $PELOTON_CSV_TIMEOUT)",
    )
    args = parser.parse_args()

    env = dict(os.environ)
    if args.output_dir:
        env[ENV_KEY] = args.output_dir
    try:
        output_dir = str(ensure_csv_dir(resolve_csv_dir(env)))
    except CsvDirError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context_kwargs = {
            "viewport": {"width": 1280, "height": 900},
            "accept_downloads": True,
        }
        if has_storage_state():
            context_kwargs["storage_state"] = STORAGE_STATE_PATH
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        # Navigate to workouts page
        print("Navigating to Peloton workouts page...", file=sys.stderr)
        page.goto(WORKOUTS_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(2)
        dismiss_cookie_banner(page)

        # Handle login if needed
        if not is_logged_in(page):
            print("Not logged in — running auth flow...", file=sys.stderr)
            try:
                do_login(page)
                save_storage_state(context)
                page.goto(WORKOUTS_URL, wait_until="domcontentloaded", timeout=30000)
                time.sleep(2)
                dismiss_cookie_banner(page)
            except Exception as e:
                print(f"ERROR: Auth failed: {e}", file=sys.stderr)
                browser.close()
                sys.exit(1)

        # Wait for the DOWNLOAD WORKOUTS button
        download_btn = page.locator('button:has-text("Download Workouts")')
        try:
            download_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            # Try alternate selectors
            download_btn = page.locator('button:has-text("DOWNLOAD WORKOUTS")')
            try:
                download_btn.wait_for(state="visible", timeout=5000)
            except Exception:
                print(
                    "ERROR: 'Download Workouts' button not found on the page.",
                    file=sys.stderr,
                )
                browser.close()
                sys.exit(1)

        # Click and capture the download; the watchdog covers click to file on disk
        print("Clicking download button...", file=sys.stderr)
        watchdog = start_watchdog(args.timeout, output_dir)
        with page.expect_download(timeout=30000) as download_info:
            download_btn.click()

        download = download_info.value
        suggested_name = download.suggested_filename or "peloton_workouts.csv"
        dest_path = os.path.join(output_dir, os.path.basename(suggested_name))

        # Write under a temporary name, then rename, so a reader never sees
        # a half-written CSV.
        part_path = dest_path + ".part"
        download.save_as(part_path)
        if os.path.getsize(part_path) == 0:
            os.remove(part_path)
            watchdog.cancel()
            print("ERROR: Peloton returned an empty CSV.", file=sys.stderr)
            browser.close()
            sys.exit(1)
        os.replace(part_path, dest_path)
        watchdog.cancel()
        save_storage_state(context)
        browser.close()

    print(dest_path)


if __name__ == "__main__":
    main()
