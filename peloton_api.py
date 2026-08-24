"""Peloton REST API client.

The workout CSV export has no workout ID column, so anything that needs to map
an Airtable row back to `members.onepeloton.com/profile/workouts/<id>` has to
ask the API. This module is that path.

Auth: Peloton retired the old `POST /auth/login` endpoint (it now answers 403
"Endpoint no longer accepting requests"), so the only usable credential is the
Auth0 access token the members web app stashes in `localStorage`. Playwright
already persists that store for us in `storage_state.json`, which means the
browser session captured by `auth.py` doubles as an API credential.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API_BASE = "https://api.onepeloton.com"
API_AUDIENCE = "api.onepeloton.com"
AUTH0_STORAGE_PREFIX = "@@auth0spajs@@"

# Peloton caps `limit` at 100 per page.
MAX_PAGE_SIZE = 100

# Treat a token expiring within this many seconds as already dead, so a long
# backfill can't have the token die out from under it mid-run.
TOKEN_EXPIRY_MARGIN_SECONDS = 300

USER_AGENT = "peloton-workout-extract (+https://github.com/rickarm/peloton-workout-extract)"


class PelotonAPIError(RuntimeError):
    """Raised when the Peloton API rejects a request or auth is unusable."""


# ---------------------------------------------------------------------------
# Token extraction
# ---------------------------------------------------------------------------

def _iter_local_storage(storage_state: dict):
    for origin in storage_state.get("origins", []):
        for entry in origin.get("localStorage", []):
            yield entry.get("name", ""), entry.get("value", "")


def extract_access_token(storage_state: dict) -> tuple[str | None, int | None]:
    """Pull the API access token out of a Playwright storage state.

    Returns `(token, expires_at_epoch)`. Either element is None when the entry
    is missing or unparseable — callers decide whether that warrants a browser
    round trip.
    """
    for name, raw in _iter_local_storage(storage_state):
        if not name.startswith(AUTH0_STORAGE_PREFIX) or API_AUDIENCE not in name:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        token = (parsed.get("body") or {}).get("access_token")
        if not token:
            continue
        expires_at = parsed.get("expiresAt")
        if not isinstance(expires_at, int):
            expires_at = None
        return token, expires_at
    return None, None


def token_is_fresh(expires_at: int | None, now: float | None = None) -> bool:
    """True when a token has more than the safety margin of life left."""
    if expires_at is None:
        return False
    now = time.time() if now is None else now
    return expires_at - now > TOKEN_EXPIRY_MARGIN_SECONDS


def load_storage_state(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def read_cached_token(path: str) -> tuple[str | None, int | None]:
    """Read the token from a storage-state file, tolerating a missing file."""
    if not os.path.isfile(path):
        return None, None
    try:
        return extract_access_token(load_storage_state(path))
    except (OSError, ValueError):
        return None, None


# ---------------------------------------------------------------------------
# Timestamp handling
# ---------------------------------------------------------------------------

def format_workout_timestamp(start_time: int, tz_name: str | None) -> str:
    """Render a workout start as the CSV export's `Workout Timestamp` value.

    The export writes local wall-clock time for the timezone the workout was
    recorded in — `2026-08-21 13:40 (EDT)` — and the downstream Airtable sync
    strips the parenthetical before merging. So `YYYY-MM-DD HH:MM` in the
    workout's own timezone is exactly the merge key, no normalisation needed.

    Falls back to UTC when Peloton reports a timezone this machine's tz
    database doesn't know; the caller can spot that via `timezone` in the
    record.
    """
    tz = timezone.utc
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.utc
    return datetime.fromtimestamp(start_time, tz).strftime("%Y-%m-%d %H:%M")


def to_iso_utc(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# Peloton stamps freestyle / Apple-Health-imported workouts with an all-zero
# ride id rather than omitting the join, which would otherwise link them all to
# one phantom class in Airtable.
NULL_CLASS_ID = "0" * 32


def normalize_workout(raw: dict) -> dict:
    """Flatten one API workout into the record this tool emits."""
    ride = raw.get("ride") or {}
    class_id = ride.get("id")
    if class_id == NULL_CLASS_ID:
        class_id = None
    start_time = raw.get("start_time")
    return {
        "workout_id": raw.get("id"),
        "workout_timestamp": (
            format_workout_timestamp(start_time, raw.get("timezone"))
            if start_time is not None
            else None
        ),
        "start_time_utc": to_iso_utc(start_time),
        "timezone": raw.get("timezone"),
        "fitness_discipline": raw.get("fitness_discipline"),
        "workout_type": raw.get("workout_type"),
        "status": raw.get("status"),
        "device_type": raw.get("device_type"),
        "class_id": class_id,
        "class_title": ride.get("title"),
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class PelotonAPI:
    """Minimal read-only client for the endpoints this tool needs."""

    def __init__(self, access_token: str, timeout: int = 30, page_pause: float = 0.25):
        self.access_token = access_token
        self.timeout = timeout
        self.page_pause = page_pause
        # A handful of instructors account for nearly every class, and a
        # backfill resolves the same few hundreds of times.
        self._instructor_names: dict[str, str | None] = {}

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{API_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Peloton-Platform": "web",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise PelotonAPIError(
                    f"Peloton API rejected the session ({exc.code}). "
                    f"Re-run with --refresh-session to mint a new token. {detail}"
                ) from exc
            raise PelotonAPIError(f"GET {path} failed ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise PelotonAPIError(f"GET {path} failed: {exc.reason}") from exc

    def me(self) -> dict:
        return self._get("/api/me")

    def user_id(self) -> str:
        user = self.me()
        uid = user.get("id")
        if not uid:
            raise PelotonAPIError("Peloton /api/me returned no user id")
        return uid

    def workout(self, workout_id: str) -> dict:
        return self._get(f"/api/workout/{workout_id}")

    def ride(self, class_id: str) -> dict:
        return self._get(f"/api/ride/{class_id}")

    def ride_details(self, class_id: str) -> dict:
        """Ride plus its planned power-zone segments and embedded instructor."""
        return self._get(f"/api/ride/{class_id}/details")

    def instructor_name(self, instructor_id: str | None) -> str | None:
        """Resolve an instructor id to a display name, caching hits and misses.

        A miss is cached too: an instructor Peloton no longer serves would
        otherwise be re-requested once per class that references them.
        """
        if not instructor_id:
            return None
        if instructor_id in self._instructor_names:
            return self._instructor_names[instructor_id]
        name = _instructor_display_name(self._get(f"/api/instructor/{instructor_id}"))
        self._instructor_names[instructor_id] = name
        return name

    def class_id_for_workout(self, workout_id: str) -> str | None:
        """The class a workout was taken from, or None when it had none.

        Freestyle and Apple-Health workouts carry the all-zero sentinel rather
        than omitting the join; forcing those onto a class would point every
        one of them at the same phantom row.
        """
        ride = self.workout(workout_id).get("ride") or {}
        class_id = ride.get("id")
        return None if class_id == NULL_CLASS_ID else class_id

    def class_target_zones(self, class_id: str) -> dict[int, int]:
        """Planned seconds per power zone for a class."""
        return sum_target_zones(self.ride_details(class_id))

    def resolve_class(self, class_id: str, tz_name: str | None = None) -> dict:
        """Normalized class metadata, in one request where Peloton allows it.

        `/api/ride/<id>/details` already embeds the ride and its instructor, so
        the common path costs a single call. The `/api/instructor/<id>` lookup
        is only reached when that embed is absent.
        """
        if not class_id or class_id == NULL_CLASS_ID:
            raise ValueError("resolve_class needs a real class id, not the null sentinel")
        details = self.ride_details(class_id)
        ride = details.get("ride") or {}
        embedded = ride.get("instructor") or {}
        instructor = _instructor_display_name(embedded) if embedded else None
        if instructor is None:
            instructor = self.instructor_name(ride.get("instructor_id"))
        duration = ride.get("duration")
        discipline = ride.get("fitness_discipline") or "cycling"
        return {
            "class_id": class_id,
            "title": ride.get("title"),
            "instructor": instructor,
            "instructor_id": ride.get("instructor_id"),
            "duration_sec": duration,
            "duration_min": round(duration / 60) if isinstance(duration, int) else None,
            "fitness_discipline": ride.get("fitness_discipline"),
            "original_air_time": ride.get("original_air_time"),
            "scheduled_start_time": ride.get("scheduled_start_time"),
            "class_timestamp": format_class_air_time(class_air_time(ride), tz_name),
            "difficulty_rating_avg": ride.get("difficulty_rating_avg"),
            "description": ride.get("description"),
            "image_url": ride.get("image_url"),
            "is_power_zone_class": details.get("is_power_zone_class"),
            "is_ftp_test": details.get("is_ftp_test"),
            "class_types": [
                t.get("name") for t in (details.get("class_types") or []) if t.get("name")
            ],
            "zones": sum_target_zones(details),
            "segments": target_segments(details),
            "class_url": CLASS_URL_TEMPLATE.format(discipline=discipline, class_id=class_id),
        }

    def iter_workout_pages(
        self, user_id: str, page_size: int = MAX_PAGE_SIZE, joins: str = "ride"
    ):
        """Yield pages of raw workouts, newest-first, until Peloton runs out.

        Pages rather than individual workouts because callers need to reason
        about a whole page before deciding to stop: Peloton orders by creation
        time, and a workout imported after the fact (Apple Health, a late
        device sync) can carry a `start_time` older than the record before it.
        Stopping on the first out-of-range workout would silently truncate.
        """
        page_size = max(1, min(page_size, MAX_PAGE_SIZE))
        page = 0
        while True:
            params = {"limit": page_size, "page": page, "sort_by": "-created"}
            if joins:
                params["joins"] = joins
            payload = self._get(f"/api/user/{user_id}/workouts", params)
            batch = payload.get("data") or []
            if batch:
                yield batch
            page += 1
            if page >= (payload.get("page_count") or 0) or not batch:
                return
            if self.page_pause:
                time.sleep(self.page_pause)


# ---------------------------------------------------------------------------
# Class metadata
# ---------------------------------------------------------------------------

# `Peloton-Rides` rows link to the stable class-detail modal rather than to a
# particular workout, so the URL stays valid for a class Rick has not taken.
CLASS_URL_TEMPLATE = (
    "https://members.onepeloton.com/classes/{discipline}"
    "?modal=classDetailsModal&classId={class_id}"
)

# The timezone `Peloton-Rides` renders `ClassTimestamp` in. Peloton stores
# `original_air_time` as a UTC epoch and has no opinion about how to display it.
DEFAULT_CLASS_TZ = "America/Los_Angeles"


def format_class_air_time(epoch: int | None, tz_name: str | None = None) -> str | None:
    """Render a class air time as Airtable's `YYYY-MM-DD HH:mm (ZZ)`.

    Same output shape as `peloton_extract.format_class_timestamp`, but from the
    API's UTC epoch instead of a scraped display string. `tz_name` defaults to
    UTC so the library stays deterministic; callers that write to Airtable pass
    `DEFAULT_CLASS_TZ`.
    """
    if epoch is None:
        return None
    tz = timezone.utc
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone.utc
    moment = datetime.fromtimestamp(epoch, tz)
    # "%z" gives "-0700"; Airtable stores the hour only.
    return f"{moment.strftime('%Y-%m-%d %H:%M')} ({moment.strftime('%z')[:3]})"


def class_air_time(ride: dict) -> int | None:
    """The epoch a class is keyed by: its scheduled slot, not when video rolled.

    Peloton exposes both. `original_air_time` is when the stream actually
    started, which lands a few minutes early and carries seconds
    (`2026-01-02 22:25:05Z`); `scheduled_start_time` is the round slot the
    class is published and referenced as (`22:30:00Z`). `Peloton-Rides` keys
    on the scheduled slot, so using the air time would mint a near-duplicate
    row for every class already in the table.
    """
    scheduled = ride.get("scheduled_start_time")
    if isinstance(scheduled, int) and scheduled > 0:
        return scheduled
    return ride.get("original_air_time")


def sum_target_zones(details: dict) -> dict[int, int]:
    """Total planned seconds per power zone from a `/api/ride/<id>/details` body.

    Segment offsets are **inclusive** on both ends, so a segment running 60..359
    is 300 seconds, not 299. Getting that wrong under-counts every class by one
    second per segment, which is small enough to look like rounding and large
    enough to stop the totals reconciling against the scraper's stored values.

    Segments whose offsets are missing or inverted are skipped rather than
    allowed to contribute a negative duration.
    """
    zones: dict[int, int] = {}
    metrics = (details.get("target_metrics_data") or {}).get("target_metrics") or []
    for segment in metrics:
        offsets = segment.get("offsets") or {}
        start, end = offsets.get("start"), offsets.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            continue
        seconds = end - start + 1
        for metric in segment.get("metrics") or []:
            if metric.get("name") != "power_zone":
                continue
            zone = metric.get("lower")
            if isinstance(zone, int):
                zones[zone] = zones.get(zone, 0) + seconds
    return zones


def target_segments(details: dict) -> list[dict]:
    """The ordered power-zone plan from a `/api/ride/<id>/details` body.

    Distinct from `sum_target_zones()`, which rolls the same data up per zone.
    The sequence is what shows the *shape* of a ride — three separate minutes in
    zone 5 read very differently from one three-minute block — so a repeated
    zone stays as separate entries here and is only collapsed by the totals.

    Offsets are inclusive on both ends, so `duration_sec` is `end - start + 1`.
    """
    segments: list[dict] = []
    metrics = (details.get("target_metrics_data") or {}).get("target_metrics") or []
    for segment in metrics:
        offsets = segment.get("offsets") or {}
        start, end = offsets.get("start"), offsets.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or end < start:
            continue
        for metric in segment.get("metrics") or []:
            if metric.get("name") != "power_zone":
                continue
            zone = metric.get("lower")
            if isinstance(zone, int):
                segments.append({
                    "zone": zone,
                    "start_sec": start,
                    "end_sec": end,
                    "duration_sec": end - start + 1,
                })
    return segments


def _instructor_display_name(payload: dict) -> str | None:
    name = (payload.get("name") or "").strip()
    if name:
        return name
    parts = [payload.get("first_name") or "", payload.get("last_name") or ""]
    joined = " ".join(p for p in parts if p).strip()
    return joined or None
