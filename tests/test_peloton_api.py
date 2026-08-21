"""Unit tests for the pure parts of the Peloton API client.

Nothing here touches the network or a browser — the point is to pin the two
things that silently break the Airtable join: token extraction from the
Playwright storage state, and the workout-timestamp merge key.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import peloton_api  # noqa: E402

AUDIENCE_KEY = (
    "@@auth0spajs@@::WVoJxVDdPoFx4RNewvvg6ch2mZ7bwnsM"
    "::https://api.onepeloton.com/::openid profile email offline_access"
)


def storage_state(token="tok-123", expires_at=1787428931, key=AUDIENCE_KEY):
    return {
        "cookies": [],
        "origins": [
            {
                "origin": "https://members.onepeloton.com",
                "localStorage": [
                    {"name": "ajs_user_id", "value": "\"abc\""},
                    {
                        "name": key,
                        "value": json.dumps(
                            {
                                "body": {
                                    "access_token": token,
                                    "expires_in": 172800,
                                    "refresh_token": "refresh-abc",
                                },
                                "expiresAt": expires_at,
                            }
                        ),
                    },
                ],
            }
        ],
    }


# --- token extraction ------------------------------------------------------

def test_extract_access_token_finds_api_audience_entry():
    token, expires_at = peloton_api.extract_access_token(storage_state())
    assert token == "tok-123"
    assert expires_at == 1787428931


def test_extract_access_token_ignores_non_api_auth0_entries():
    # The `@@user@@` entry holds an id_token, not an API access token.
    state = storage_state(key="@@auth0spajs@@::clientid::@@user@@")
    assert peloton_api.extract_access_token(state) == (None, None)


def test_extract_access_token_survives_unparseable_value():
    state = storage_state()
    state["origins"][0]["localStorage"][1]["value"] = "not json"
    assert peloton_api.extract_access_token(state) == (None, None)


def test_extract_access_token_on_empty_state():
    assert peloton_api.extract_access_token({}) == (None, None)


def test_read_cached_token_tolerates_missing_file(tmp_path):
    missing = tmp_path / "nope.json"
    assert peloton_api.read_cached_token(str(missing)) == (None, None)


def test_read_cached_token_reads_a_real_file(tmp_path):
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(storage_state()))
    assert peloton_api.read_cached_token(str(path)) == ("tok-123", 1787428931)


# --- expiry ----------------------------------------------------------------

def test_token_is_fresh_when_well_ahead():
    assert peloton_api.token_is_fresh(2000, now=1000)


def test_token_is_stale_inside_the_safety_margin():
    margin = peloton_api.TOKEN_EXPIRY_MARGIN_SECONDS
    assert not peloton_api.token_is_fresh(1000 + margin - 1, now=1000)


def test_token_is_stale_when_expiry_unknown():
    assert not peloton_api.token_is_fresh(None, now=1000)


# --- merge key -------------------------------------------------------------

def test_format_workout_timestamp_uses_the_workouts_own_timezone():
    # 1787334007 == 2026-08-21 17:40 UTC; recorded on Eastern time.
    assert (
        peloton_api.format_workout_timestamp(1787334007, "America/New_York")
        == "2026-08-21 13:40"
    )


def test_format_workout_timestamp_handles_etc_gmt_offsets():
    # Peloton emits POSIX-style `Etc/GMT+7`, which means UTC-7.
    assert (
        peloton_api.format_workout_timestamp(1786748728, "Etc/GMT+7")
        == "2026-08-14 16:05"
    )


def test_format_workout_timestamp_falls_back_to_utc_on_unknown_zone():
    assert (
        peloton_api.format_workout_timestamp(1787334007, "Mars/Olympus_Mons")
        == "2026-08-21 17:40"
    )


def test_format_workout_timestamp_falls_back_to_utc_when_zone_missing():
    assert peloton_api.format_workout_timestamp(1787334007, None) == "2026-08-21 17:40"


# --- normalisation ---------------------------------------------------------

RAW_WORKOUT = {
    "id": "e06aeba77b284dcaafe5aa0973e5de6a",
    "start_time": 1787334007,
    "created_at": 1787333977,
    "timezone": "America/New_York",
    "fitness_discipline": "strength",
    "workout_type": "class",
    "status": "COMPLETE",
    "device_type": "iPhone",
    "ride": {"id": "36127bcaa9484b4cb8e450dd4807f8bc", "title": "10 min Chest & Back Strength"},
}


def test_normalize_workout_maps_the_fields_the_sync_needs():
    record = peloton_api.normalize_workout(RAW_WORKOUT)
    assert record == {
        "workout_id": "e06aeba77b284dcaafe5aa0973e5de6a",
        "workout_timestamp": "2026-08-21 13:40",
        "start_time_utc": "2026-08-21T17:40:07Z",
        "timezone": "America/New_York",
        "fitness_discipline": "strength",
        "workout_type": "class",
        "status": "COMPLETE",
        "device_type": "iPhone",
        "class_id": "36127bcaa9484b4cb8e450dd4807f8bc",
        "class_title": "10 min Chest & Back Strength",
    }


def test_normalize_workout_without_a_class_join():
    raw = {k: v for k, v in RAW_WORKOUT.items() if k != "ride"}
    record = peloton_api.normalize_workout(raw)
    assert record["class_id"] is None
    assert record["class_title"] is None
    assert record["workout_id"] == RAW_WORKOUT["id"]


def test_normalize_workout_without_a_start_time():
    raw = {k: v for k, v in RAW_WORKOUT.items() if k != "start_time"}
    record = peloton_api.normalize_workout(raw)
    assert record["workout_timestamp"] is None
    assert record["start_time_utc"] is None


# --- pagination ------------------------------------------------------------

class FakeAPI(peloton_api.PelotonAPI):
    """PelotonAPI with the HTTP layer swapped for canned pages."""

    def __init__(self, pages):
        super().__init__("tok", page_pause=0)
        self.pages = pages
        self.requested = []

    def _get(self, path, params=None):
        page = (params or {}).get("page", 0)
        self.requested.append(page)
        data = self.pages[page] if page < len(self.pages) else []
        return {"data": data, "page_count": len(self.pages), "page": page}


def test_iter_workout_pages_walks_every_page():
    api = FakeAPI([[{"id": "a"}, {"id": "b"}], [{"id": "c"}]])
    pages = list(api.iter_workout_pages("u1"))
    assert [[w["id"] for w in p] for p in pages] == [["a", "b"], ["c"]]
    assert api.requested == [0, 1]


def test_iter_workout_pages_stops_on_an_empty_result():
    api = FakeAPI([[]])
    assert list(api.iter_workout_pages("u1")) == []


def test_iter_workout_pages_clamps_page_size_to_the_api_maximum():
    api = FakeAPI([[{"id": "a"}]])
    list(api.iter_workout_pages("u1", page_size=10_000))
    # Nothing to assert on the fake beyond it not blowing up; the clamp is
    # visible in the params the real client would send.
    assert api.requested == [0]


# --- collection bounds -----------------------------------------------------

import peloton_workout_ids as cli  # noqa: E402


def _w(wid, start_time):
    return {
        "id": wid,
        "start_time": start_time,
        "timezone": "UTC",
        "fitness_discipline": "cycling",
        "workout_type": "class",
        "status": "COMPLETE",
    }


def test_collect_workouts_honours_limit_and_stops_paging():
    api = FakeAPI([[_w("a", 300), _w("b", 200)], [_w("c", 100)]])
    records = cli.collect_workouts(api, "u1", limit=2, since_epoch=None)
    assert [r["workout_id"] for r in records] == ["a", "b"]
    assert api.requested == [0]


def test_collect_workouts_returns_everything_when_unbounded():
    api = FakeAPI([[_w("a", 300)], [_w("b", 200)], [_w("c", 100)]])
    records = cli.collect_workouts(api, "u1", limit=None, since_epoch=None)
    assert [r["workout_id"] for r in records] == ["a", "b", "c"]


def test_collect_workouts_since_keeps_out_of_order_records_on_a_mixed_page():
    # `b` starts before the cutoff but `c`, behind it in creation order, does
    # not. Stopping at the first old record would lose `c`.
    api = FakeAPI([[_w("a", 300), _w("b", 100), _w("c", 250)], [_w("d", 50)]])
    records = cli.collect_workouts(api, "u1", limit=None, since_epoch=200)
    assert sorted(r["workout_id"] for r in records) == ["a", "c"]


def test_collect_workouts_since_stops_after_a_fully_old_page():
    api = FakeAPI([[_w("a", 300)], [_w("b", 100)], [_w("c", 90)]])
    cli.collect_workouts(api, "u1", limit=None, since_epoch=200)
    assert api.requested == [0, 1]


def test_collect_workouts_sorts_by_start_time_not_creation_order():
    api = FakeAPI([[_w("late", 100), _w("early", 900)]])
    records = cli.collect_workouts(api, "u1", limit=None, since_epoch=None)
    assert [r["workout_id"] for r in records] == ["early", "late"]


# --- rendering -------------------------------------------------------------

def test_render_csv_emits_the_documented_columns():
    records = [peloton_api.normalize_workout(RAW_WORKOUT)]
    out = cli.render(records, "csv").splitlines()
    assert out[0] == ",".join(cli.CSV_COLUMNS)
    assert out[1].startswith("e06aeba77b284dcaafe5aa0973e5de6a,2026-08-21 13:40,")


def test_render_jsonl_is_one_object_per_line():
    records = [peloton_api.normalize_workout(RAW_WORKOUT)] * 2
    lines = cli.render(records, "jsonl").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["workout_id"] == RAW_WORKOUT["id"]


def test_render_json_wraps_records_with_run_metadata():
    payload = json.loads(cli.render([peloton_api.normalize_workout(RAW_WORKOUT)], "json"))
    assert payload["run_metadata"]["total_workouts"] == 1
    assert payload["workouts"][0]["class_id"] == RAW_WORKOUT["ride"]["id"]


def test_parse_since_returns_a_utc_epoch():
    assert cli.parse_since("2026-08-21") == 1787270400


def test_parse_since_rejects_a_bad_date():
    import argparse as _argparse

    try:
        cli.parse_since("08/21/2026")
    except _argparse.ArgumentTypeError:
        return
    raise AssertionError("expected ArgumentTypeError")


def test_normalize_workout_drops_the_all_zero_placeholder_class_id():
    raw = dict(RAW_WORKOUT, ride={"id": "0" * 32, "title": "13 min Outdoor Cycling"})
    record = peloton_api.normalize_workout(raw)
    assert record["class_id"] is None
    assert record["class_title"] == "13 min Outdoor Cycling"
