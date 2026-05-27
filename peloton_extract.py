#!/usr/bin/env python3
"""Extract structured metadata from Peloton workout detail pages.

Usage:
    peloton_extract <url> [<url> ...] [--output-file PATH] [--dry-run] [--headed] [--format json|jsonl]
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, Page, BrowserContext

from pel_selectors import SELECTORS
from auth import (
    CACHE_DIR,
    STORAGE_STATE_PATH,
    dismiss_cookie_banner,
    do_login,
    has_storage_state,
    is_logged_in,
    save_storage_state,
)

VERSION = "1.0.0"
URL_PATTERN = re.compile(
    r"https://members\.onepeloton\.com/profile/workouts/([a-f0-9]{32})"
)

log = logging.getLogger("peloton_extract")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def setup_logging(debug_log_dir: str) -> None:
    os.makedirs(debug_log_dir, exist_ok=True)
    log_file = os.path.join(debug_log_dir, f"{datetime.now():%Y-%m-%d}.log")

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(log_file),
        ],
    )
    logging.getLogger("peloton_extract").setLevel(logging.DEBUG)
    # Quiet noisy libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_workout_id(url: str) -> str | None:
    m = URL_PATTERN.match(url.strip())
    return m.group(1) if m else None


def mm_ss_to_seconds(mm_ss: str) -> int:
    parts = mm_ss.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def seconds_to_mm_ss(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def parse_segment_duration_text(text: str) -> int:
    """Parse '12 min' or '1 min' to seconds."""
    m = re.match(r"(\d+)\s*min", text.strip())
    return int(m.group(1)) * 60 if m else 0


# Timezone abbreviation → UTC offset hours
_TZ_OFFSETS = {
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "ET": -5, "CT": -6, "MT": -7, "PT": -8,
}


def format_class_timestamp(raw: str) -> str:
    """Convert Peloton class timestamp to 'YYYY-MM-DD HH:mm (ZZ)' format.

    Input examples:
        "Thursday, April 17, 2026 @ 7:00 AM PDT"
        "Fri 4/2/26 @ 3:30 AM"
    Output: "2026-04-17 07:00 (-07)"
    """
    if not raw:
        return raw

    # Strip day-of-week prefix (e.g. "Thursday, " or "Thu ")
    text = re.sub(r"^[A-Za-z]+,?\s*", "", raw.strip())

    # Extract timezone abbreviation from the end
    tz_match = re.search(r"\b([A-Z]{2,4})$", text)
    tz_abbr = tz_match.group(1) if tz_match else ""
    if tz_abbr:
        text = text[:tz_match.start()].strip()

    # Try parsing common Peloton formats
    for fmt in (
        "%B %d, %Y @ %I:%M %p",   # "April 17, 2026 @ 7:00 AM"
        "%b %d, %Y @ %I:%M %p",   # "Apr 17, 2026 @ 7:00 AM"
        "%m/%d/%y @ %I:%M %p",    # "4/2/26 @ 3:30 AM"
        "%m/%d/%Y @ %I:%M %p",    # "4/2/2026 @ 3:30 AM"
    ):
        try:
            dt = datetime.strptime(text, fmt)
            offset = _TZ_OFFSETS.get(tz_abbr)
            if offset is not None:
                offset_str = f"{offset:+03d}"
            else:
                offset_str = tz_abbr or "?"
            return f"{dt:%Y-%m-%d %H:%M} ({offset_str})"
        except ValueError:
            continue

    # Fallback: return raw value unchanged
    log.warning(f"Could not parse class timestamp: {raw!r}")
    return raw


# ---------------------------------------------------------------------------
# Page data extraction
# ---------------------------------------------------------------------------

def extract_workout_metadata(page: Page, workout_id: str) -> dict:
    """Extract basic metadata from the workout detail page."""
    result = {
        "workout_id": workout_id,
        "extraction_status": "ok",
        "extraction_warnings": [],
    }

    # Workout timestamp — first div child inside workoutBasicInfoBlock
    info_block = page.locator(SELECTORS["workout_info_block"])
    if info_block.count() == 0:
        result["extraction_status"] = "failed"
        result["extraction_warnings"].append("workoutBasicInfoBlock not found")
        return result

    # The timestamp is in a div before the h1 title
    ts_div = info_block.locator("div").first
    # Navigate into the inner structure: the block has nested divs
    # Structure: workoutBasicInfoBlock > div > div.timestamp, h1.title, spans
    inner = info_block.locator("> div > div").first
    raw_ts = inner.text_content().strip() if inner.count() > 0 else ""
    result["workout_timestamp"] = raw_ts.replace("\xa0", " ")

    # Ride title — scoped to workout block (classTitle appears in both blocks)
    title_el = info_block.locator(SELECTORS["ride_title"])
    result["ride_title"] = title_el.text_content().strip() if title_el.count() > 0 else ""

    # Duration from title (e.g. "75 min Power Zone Endurance Ride")
    dur_match = re.match(r"(\d+)\s*min", result["ride_title"])
    result["duration_minutes"] = int(dur_match.group(1)) if dur_match else 0
    result["total_duration_seconds"] = result["duration_minutes"] * 60

    # Discipline + instructor from subtitle spans
    subtitle_spans = info_block.locator("span")
    discipline_text = ""
    instructor_text = ""
    for i in range(subtitle_spans.count()):
        text = subtitle_spans.nth(i).text_content().strip()
        if "Workout" in text:
            discipline_text = text
        elif text and text != "·" and not text.startswith("BIKE") and "Workout" not in text:
            # Could be instructor or device
            if text.isupper() or (len(text) > 3 and text[0].isupper()):
                # Check if it's a known device
                if text in ("BIKE", "BIKE+", "TREAD", "TREAD+", "ROW", "GUIDE"):
                    result["device"] = text
                elif not instructor_text:
                    instructor_text = text

    # Detect discipline from the subtitle
    disc_map = {
        "Cycling Workout": "cycling",
        "Strength Workout": "strength",
        "Yoga Workout": "yoga",
        "Meditation Workout": "meditation",
        "Tread Workout": "tread",
        "Rowing Workout": "rowing",
        "Walking Workout": "walking",
        "Running Workout": "running",
        "Stretching Workout": "stretching",
        "Cardio Workout": "cardio",
        "Bootcamp Workout": "bootcamp",
    }
    result["discipline"] = disc_map.get(discipline_text, discipline_text.lower().replace(" workout", "") if discipline_text else "unknown")

    # Instructor from class info block (more reliable)
    class_info = page.locator(SELECTORS["class_info_block"])
    if class_info.count() > 0:
        # Instructor name is in a span near the instructor photo
        # Structure: classBasicInfoBlock > a[instructorPhoto] img, then sibling div with spans
        inst_img = class_info.locator(SELECTORS["instructor_photo"] + " img")
        if inst_img.count() > 0:
            result["instructor"] = inst_img.get_attribute("alt") or ""
        elif instructor_text:
            result["instructor"] = instructor_text
        else:
            result["instructor"] = ""

        # Class timestamp — span starting with "From "
        spans = class_info.locator("span")
        for i in range(spans.count()):
            text = spans.nth(i).text_content().strip().replace("\xa0", " ")
            if text.startswith("From "):
                result["class_timestamp"] = format_class_timestamp(text[5:])
                break

        # Discipline label (second occurrence, e.g. "CYCLING")
        # Instructor · CYCLING pattern
        inner_spans = class_info.locator("div span")
        for i in range(inner_spans.count()):
            text = inner_spans.nth(i).text_content().strip()
            if text.upper() == text and len(text) > 2 and text not in ("·",):
                if text in ("CYCLING", "STRENGTH", "YOGA", "MEDITATION", "TREAD", "ROWING", "WALKING", "RUNNING", "STRETCHING", "CARDIO", "BOOTCAMP"):
                    result["discipline"] = text.lower()

    if "instructor" not in result:
        result["instructor"] = instructor_text or ""
    if "class_timestamp" not in result:
        result["class_timestamp"] = ""

    # View Class link contains the classId
    vc = page.locator(SELECTORS["view_class_button"])
    if vc.count() > 0:
        href = vc.get_attribute("href") or ""
        class_id_match = re.search(r"classId=([a-f0-9]{32})", href)
        if class_id_match:
            result["class_id"] = class_id_match.group(1)
            result["class_detail_url"] = (
                f"https://members.onepeloton.com/classes/{result['discipline']}"
                f"?modal=classDetailsModal&classId={result['class_id']}"
            )

    if "class_id" not in result:
        result["class_id"] = ""
        result["class_detail_url"] = ""

    # Notes — not available in the workout page DOM (would need API)
    result["notes"] = ""

    return result


def extract_class_plan(page: Page, metadata: dict) -> dict:
    """Navigate to class details modal, expand all segments, extract the plan."""
    warnings = list(metadata.get("extraction_warnings", []))

    # Click View Class to open the modal
    vc = page.locator(SELECTORS["view_class_button"])
    if vc.count() == 0:
        warnings.append("View Class button not found")
        metadata["extraction_warnings"] = warnings
        metadata["extraction_status"] = "partial"
        return metadata

    log.info("Clicking View Class...")
    vc.click()
    time.sleep(2)

    # Wait for modal
    modal = page.locator(SELECTORS["class_details_modal"])
    try:
        modal.wait_for(timeout=10000)
    except Exception:
        warnings.append("Class details modal did not appear")
        metadata["extraction_warnings"] = warnings
        metadata["extraction_status"] = "partial"
        return metadata

    # Click View Details
    vd = page.locator(SELECTORS["view_details_button"])
    if vd.count() == 0:
        warnings.append("View Details button not found — no class plan available")
        metadata["extraction_warnings"] = warnings
        metadata["extraction_status"] = "minimal"
        return metadata

    log.info("Clicking View Details...")
    vd.click()
    time.sleep(2)

    # Extract segments one at a time — Peloton uses an accordion, so only one
    # segment can be expanded at a time. Expand each, extract, then move on.
    segments = page.locator(SELECTORS["segment_container"])
    seg_count = segments.count()
    log.info(f"Found {seg_count} segments")

    class_plan_breakdown = []
    zone_totals = {}  # zone_name -> total_seconds

    for i in range(seg_count):
        seg_btn = segments.nth(i)

        # Get segment name and duration from the button (visible even when collapsed)
        seg_name_el = seg_btn.locator(SELECTORS["segment_name"])
        seg_dur_el = seg_btn.locator(SELECTORS["segment_duration"])
        seg_name = seg_name_el.text_content().strip() if seg_name_el.count() > 0 else f"Segment {i}"
        seg_dur_text = seg_dur_el.text_content().strip() if seg_dur_el.count() > 0 else "0 min"

        # Ensure this segment is expanded
        if seg_btn.get_attribute("aria-expanded") == "false":
            log.info(f"Expanding: {seg_name}")
            seg_btn.click()
            time.sleep(1.5)

        # Subsegments are in a sibling div of the button, scoped to the parent li
        parent_li = seg_btn.locator("xpath=ancestor::li[1]")
        sub_names = parent_li.locator(SELECTORS["subsegment_name"])
        sub_durs = parent_li.locator(SELECTORS["subsegment_duration"])

        movements = []
        sub_count = sub_names.count()
        log.info(f"  {seg_name} ({seg_dur_text}): {sub_count} movements")

        for j in range(sub_count):
            name = sub_names.nth(j).text_content().strip()
            dur = sub_durs.nth(j).text_content().strip()
            movement = {"name": name, "duration": dur}

            zone_match = re.match(r"Zone (\d)", name)
            if zone_match:
                zone_key = f"zone_{zone_match.group(1)}"
                zone_totals[zone_key] = zone_totals.get(zone_key, 0) + mm_ss_to_seconds(dur)
            else:
                movement["note"] = "Transition movement -- no zone assigned"
                zone_totals["unassigned"] = zone_totals.get("unassigned", 0) + mm_ss_to_seconds(dur)

            movements.append(movement)

        class_plan_breakdown.append({
            "segment": seg_name,
            "duration": seg_dur_text,
            "movements": movements,
        })

    # Build class_plan_zones with all 7 zones
    class_plan_zones = {}
    for z in range(1, 8):
        key = f"zone_{z}"
        class_plan_zones[key] = seconds_to_mm_ss(zone_totals.get(key, 0))
    class_plan_zones["unassigned"] = seconds_to_mm_ss(zone_totals.get("unassigned", 0))

    metadata["class_plan_zones"] = class_plan_zones
    metadata["class_plan_breakdown"] = class_plan_breakdown

    # Validate: zone totals should reconcile with duration
    total_zone_secs = sum(zone_totals.values())
    expected_secs = metadata.get("total_duration_seconds", 0)
    if expected_secs > 0 and abs(total_zone_secs - expected_secs) > 5:
        warnings.append(
            f"Zone totals ({seconds_to_mm_ss(total_zone_secs)}) don't reconcile "
            f"with duration ({seconds_to_mm_ss(expected_secs)}), "
            f"diff={abs(total_zone_secs - expected_secs)}s"
        )

    # Close the modal
    close_btn = page.locator(SELECTORS["modal_close"])
    if close_btn.count() > 0:
        close_btn.click()
        time.sleep(0.5)

    metadata["extraction_warnings"] = warnings
    return metadata


def extract_single_workout(
    page: Page,
    context: BrowserContext,
    url: str,
    workout_id: str,
    dry_run: bool,
) -> dict:
    """Extract data from a single workout URL."""
    log.info(f"Processing {workout_id}")

    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception as e:
        return {
            "workout_id": workout_id,
            "extraction_status": "failed",
            "extraction_warnings": [f"Navigation failed: {e}"],
        }

    time.sleep(1)
    dismiss_cookie_banner(page)

    # Check auth
    if not is_logged_in(page):
        log.info("Not logged in — running auth flow...")
        try:
            do_login(page)
            save_storage_state(context)
            page.goto(url, wait_until="networkidle", timeout=30000)
            time.sleep(1)
            dismiss_cookie_banner(page)
        except Exception as e:
            return {
                "workout_id": workout_id,
                "extraction_status": "failed",
                "extraction_warnings": [f"Auth failed: {e}"],
            }

    # Dry run: dump HTML + screenshot, skip parsing
    if dry_run:
        debug_dir = os.path.join(CACHE_DIR, "debug", workout_id)
        os.makedirs(debug_dir, exist_ok=True)
        page.screenshot(path=os.path.join(debug_dir, "screenshot.png"), full_page=True)
        with open(os.path.join(debug_dir, "page.html"), "w") as f:
            f.write(page.content())
        log.info(f"Dry run artifacts saved to {debug_dir}")
        return {
            "workout_id": workout_id,
            "extraction_status": "dry_run",
            "extraction_warnings": [],
            "debug_dir": debug_dir,
        }

    # Extract metadata
    metadata = extract_workout_metadata(page, workout_id)
    if metadata["extraction_status"] == "failed":
        return metadata

    # Extract class plan for cycling/tread power zone rides
    has_plan = metadata.get("discipline") in ("cycling", "tread") and "Power Zone" in metadata.get("ride_title", "")
    if has_plan:
        metadata = extract_class_plan(page, metadata)
    else:
        if metadata.get("discipline") != "cycling":
            metadata["extraction_status"] = "minimal"
            metadata["extraction_warnings"].append(
                f"No class plan available — discipline={metadata.get('discipline', 'unknown')}"
            )
        elif "Power Zone" not in metadata.get("ride_title", ""):
            metadata["extraction_status"] = "minimal"
            metadata["extraction_warnings"].append("No class plan — not a Power Zone ride")

    return metadata


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract Peloton workout data")
    parser.add_argument("urls", nargs="*", help="Workout URLs")
    parser.add_argument("--output-file", help="Write output to file")
    parser.add_argument("--dry-run", action="store_true", help="Dump HTML + screenshot, skip parsing")
    parser.add_argument("--headed", action="store_true", help="Run browser visibly")
    parser.add_argument("--format", choices=["json", "jsonl"], default="json", help="Output format")
    args = parser.parse_args()

    setup_logging(os.path.join(CACHE_DIR, "logs"))

    # Collect URLs from args or stdin
    urls = args.urls
    if not urls and not sys.stdin.isatty():
        urls = [line.strip() for line in sys.stdin if line.strip()]

    if not urls:
        parser.error("No URLs provided")

    # Validate URLs
    url_map = {}
    for url in urls:
        wid = parse_workout_id(url)
        if not wid:
            log.error(f"Invalid URL: {url}")
            continue
        url_map[wid] = url

    if not url_map:
        log.error("No valid URLs found")
        sys.exit(1)

    # Launch browser
    results = []
    succeeded = 0
    failed = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context_kwargs = {"viewport": {"width": 1280, "height": 900}}
        if has_storage_state():
            context_kwargs["storage_state"] = STORAGE_STATE_PATH
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        for i, (wid, url) in enumerate(url_map.items()):
            if i > 0:
                delay = 1.5 + (0.5 * (i % 2))  # 1.5-2.0s
                log.debug(f"Sleeping {delay}s between workouts")
                time.sleep(delay)

            result = extract_single_workout(page, context, url, wid, args.dry_run)
            results.append(result)

            if result.get("extraction_status") in ("ok", "minimal", "partial", "dry_run"):
                succeeded += 1
            else:
                failed += 1

        # Save storage state after successful run
        if succeeded > 0 and not args.dry_run:
            save_storage_state(context)

        browser.close()

    # Build output
    output = {
        "workouts": results,
        "run_metadata": {
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "skill_version": VERSION,
            "total_requested": len(url_map),
            "total_succeeded": succeeded,
            "total_failed": failed,
        },
    }

    # Emit output
    if args.format == "jsonl":
        lines = [json.dumps(w) for w in results]
        text = "\n".join(lines) + "\n"
    else:
        text = json.dumps(output, indent=2) + "\n"

    sys.stdout.write(text)

    if args.output_file:
        Path(args.output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output_file, "w") as f:
            f.write(text)
        log.info(f"Output written to {args.output_file}")

    # Exit code
    if failed == len(url_map):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
