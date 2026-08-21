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
