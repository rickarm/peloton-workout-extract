#!/usr/bin/env python3
"""Download the Peloton workout CSV via headless browser.

Usage:
    peloton_csv_download [--headed] [--output-dir DIR]
"""

import argparse
import os
import sys
import time

from playwright.sync_api import sync_playwright

from auth import (
    CACHE_DIR,
    STORAGE_STATE_PATH,
    dismiss_cookie_banner,
    do_login,
    has_storage_state,
    is_logged_in,
    save_storage_state,
)

WORKOUTS_URL = "https://members.onepeloton.com/profile/workouts"
DOWNLOAD_DIR_DEFAULT = os.path.expanduser("~/Downloads")


def main():
    parser = argparse.ArgumentParser(description="Download Peloton workout CSV")
    parser.add_argument("--headed", action="store_true", help="Run browser visibly")
    parser.add_argument(
        "--output-dir",
        default=DOWNLOAD_DIR_DEFAULT,
        help=f"Directory for downloaded CSV (default: {DOWNLOAD_DIR_DEFAULT})",
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

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

        # Click and capture the download
        print("Clicking download button...", file=sys.stderr)
        with page.expect_download(timeout=30000) as download_info:
            download_btn.click()

        download = download_info.value
        suggested_name = download.suggested_filename or "peloton_workouts.csv"
        dest_path = os.path.join(args.output_dir, suggested_name)

        download.save_as(dest_path)
        save_storage_state(context)
        browser.close()

    print(dest_path)


if __name__ == "__main__":
    main()
