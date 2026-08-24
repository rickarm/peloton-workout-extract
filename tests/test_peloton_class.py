"""Unit tests for class-metadata resolution.

No network and no browser, same policy as `test_peloton_api.py`. The payload
shapes below were captured from live API responses; the two regression cases at
the bottom are the rides quoted in issue #10, whose zone sums were verified
against the values the Playwright scraper had already stored in Airtable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import peloton_api  # noqa: E402

# --- fixtures --------------------------------------------------------------

INSTRUCTOR_ID = "35016225e39d46dbbc364991ab48e10f"
CLASS_ID = "4c20e2d1acd345bdbb2a1aa2fbaf13a3"

RIDE = {
    "id": CLASS_ID,
    "title": "45 min Power Zone Endurance Ride",
    "duration": 2700,
    "length": 2891,
    "fitness_discipline": "cycling",
    "instructor_id": INSTRUCTOR_ID,
    "original_air_time": 1742356505,
    "scheduled_start_time": 1742356800,
    "difficulty_rating_avg": 7.7,
    "description": "Ride in your endurance zones.",
    "image_url": "https://example.invalid/thumb.jpg",
    "ride_type_id": "665395ff3abf4081bf315686227d1a51",
    "class_type_ids": ["665395ff3abf4081bf315686227d1a51"],
    "ride_types": [{"id": "665395ff3abf4081bf315686227d1a51", "name": "Power Zone"}],
}

SEGMENTS = [
    {"offsets": {"start": 60, "end": 359}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 360, "end": 2639}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 2, "upper": 2}]},
]

DETAILS = {
    "ride": dict(RIDE, instructor={"id": INSTRUCTOR_ID, "name": "Christian Vande Velde"}),
    "is_power_zone_class": True,
    "is_ftp_test": False,
    "class_types": [{"id": "665395ff3abf4081bf315686227d1a51", "name": "Power Zone"}],
    "target_metrics_data": {"target_metrics": SEGMENTS, "total_expected_output": 250},
}


class FakeAPI(peloton_api.PelotonAPI):
    """Serves canned payloads and records every path requested."""

    def __init__(self, routes):
        super().__init__("tok", page_pause=0)
        self.routes = routes
        self.calls = []

    def _get(self, path, params=None):
        self.calls.append(path)
        if path not in self.routes:
            raise peloton_api.PelotonAPIError(f"unrouted path {path}")
        return self.routes[path]


def api_with_class(**overrides):
    routes = {
        f"/api/ride/{CLASS_ID}/details": DETAILS,
        f"/api/ride/{CLASS_ID}": RIDE,
        f"/api/instructor/{INSTRUCTOR_ID}": {"id": INSTRUCTOR_ID, "name": "Christian Vande Velde"},
    }
    routes.update(overrides)
    return FakeAPI(routes)


# --- workout to class id ---------------------------------------------------

def test_class_id_for_workout_reads_the_ride_join():
    api = FakeAPI({"/api/workout/w1": {"id": "w1", "ride": {"id": CLASS_ID}}})
    assert api.class_id_for_workout("w1") == CLASS_ID


def test_class_id_for_workout_maps_the_all_zero_sentinel_to_none():
    # Freestyle and Apple-Health workouts must not be forced onto a class.
    api = FakeAPI({"/api/workout/w1": {"id": "w1", "ride": {"id": "0" * 32}}})
    assert api.class_id_for_workout("w1") is None


def test_class_id_for_workout_handles_a_missing_ride_join():
    api = FakeAPI({"/api/workout/w1": {"id": "w1"}})
    assert api.class_id_for_workout("w1") is None


def test_class_id_for_workout_handles_a_null_ride_join():
    api = FakeAPI({"/api/workout/w1": {"id": "w1", "ride": None}})
    assert api.class_id_for_workout("w1") is None


# --- zone summation --------------------------------------------------------

def test_sum_target_zones_treats_offsets_as_inclusive():
    # 60..359 inclusive is 300 seconds, not 299.
    assert peloton_api.sum_target_zones(DETAILS) == {1: 300, 2: 2280}


def test_sum_target_zones_accumulates_repeated_zones():
    payload = {"target_metrics_data": {"target_metrics": [
        {"offsets": {"start": 0, "end": 9}, "metrics": [{"name": "power_zone", "lower": 3}]},
        {"offsets": {"start": 10, "end": 19}, "metrics": [{"name": "power_zone", "lower": 3}]},
    ]}}
    assert peloton_api.sum_target_zones(payload) == {3: 20}


def test_sum_target_zones_ignores_non_power_zone_metrics():
    payload = {"target_metrics_data": {"target_metrics": [
        {"offsets": {"start": 0, "end": 9}, "metrics": [{"name": "cadence", "lower": 80}]},
    ]}}
    assert peloton_api.sum_target_zones(payload) == {}


def test_sum_target_zones_on_a_class_with_no_targets():
    assert peloton_api.sum_target_zones({}) == {}
    assert peloton_api.sum_target_zones({"target_metrics_data": {}}) == {}
    assert peloton_api.sum_target_zones({"target_metrics_data": {"target_metrics": []}}) == {}


def test_sum_target_zones_skips_segments_with_broken_offsets():
    payload = {"target_metrics_data": {"target_metrics": [
        {"offsets": {"start": 10, "end": 5}, "metrics": [{"name": "power_zone", "lower": 2}]},
        {"metrics": [{"name": "power_zone", "lower": 2}]},
        {"offsets": {"start": 0, "end": 9}, "metrics": [{"name": "power_zone", "lower": 2}]},
    ]}}
    assert peloton_api.sum_target_zones(payload) == {2: 10}


def test_class_target_zones_calls_the_details_endpoint():
    api = api_with_class()
    assert api.class_target_zones(CLASS_ID) == {1: 300, 2: 2280}
    assert api.calls == [f"/api/ride/{CLASS_ID}/details"]


# --- class resolution ------------------------------------------------------

def test_resolve_class_returns_the_fields_the_sync_needs():
    record = api_with_class().resolve_class(CLASS_ID)
    assert record["class_id"] == CLASS_ID
    assert record["title"] == "45 min Power Zone Endurance Ride"
    assert record["instructor"] == "Christian Vande Velde"
    assert record["duration_min"] == 45
    assert record["fitness_discipline"] == "cycling"
    assert record["is_power_zone_class"] is True
    assert record["is_ftp_test"] is False
    assert record["class_types"] == ["Power Zone"]
    assert record["zones"] == {1: 300, 2: 2280}
    assert record["class_url"] == f"https://members.onepeloton.com/classes/cycling?modal=classDetailsModal&classId={CLASS_ID}"


def test_resolve_class_formats_the_air_time_for_airtable():
    # Peloton-Rides keys on `YYYY-MM-DD HH:mm (ZZ)`.
    record = api_with_class().resolve_class(CLASS_ID, tz_name="America/Los_Angeles")
    assert record["class_timestamp"] == "2025-03-18 21:00 (-07)"


def test_resolve_class_keys_on_the_scheduled_slot_not_the_stream_start():
    # original_air_time is 4m55s earlier and would mint a near-duplicate row.
    record = api_with_class().resolve_class(CLASS_ID, tz_name="America/Los_Angeles")
    assert record["class_timestamp"].endswith("21:00 (-07)")
    assert record["scheduled_start_time"] == 1742356800
    assert record["original_air_time"] == 1742356505


def test_class_air_time_falls_back_when_no_slot_is_published():
    assert peloton_api.class_air_time({"original_air_time": 999}) == 999
    assert peloton_api.class_air_time({"scheduled_start_time": 0, "original_air_time": 999}) == 999
    assert peloton_api.class_air_time({}) is None


def test_resolve_class_uses_one_request_when_details_embeds_the_instructor():
    api = api_with_class()
    api.resolve_class(CLASS_ID)
    assert api.calls == [f"/api/ride/{CLASS_ID}/details"]


def test_resolve_class_falls_back_to_the_instructor_endpoint():
    stripped = dict(DETAILS, ride=dict(RIDE))  # no embedded instructor
    api = api_with_class(**{f"/api/ride/{CLASS_ID}/details": stripped})
    record = api.resolve_class(CLASS_ID)
    assert record["instructor"] == "Christian Vande Velde"
    assert f"/api/instructor/{INSTRUCTOR_ID}" in api.calls


def test_resolve_class_tolerates_an_unresolvable_instructor():
    stripped = dict(DETAILS, ride={k: v for k, v in RIDE.items() if k != "instructor_id"})
    api = api_with_class(**{f"/api/ride/{CLASS_ID}/details": stripped})
    assert api.resolve_class(CLASS_ID)["instructor"] is None


def test_resolve_class_rejects_the_null_sentinel():
    try:
        api_with_class().resolve_class("0" * 32)
    except ValueError:
        return
    raise AssertionError("expected ValueError for the all-zero class id")


# --- instructor cache ------------------------------------------------------

def test_instructor_name_is_cached_across_calls():
    api = api_with_class()
    assert api.instructor_name(INSTRUCTOR_ID) == "Christian Vande Velde"
    assert api.instructor_name(INSTRUCTOR_ID) == "Christian Vande Velde"
    assert api.calls.count(f"/api/instructor/{INSTRUCTOR_ID}") == 1


def test_instructor_name_caches_a_miss_so_it_is_not_retried():
    api = FakeAPI({"/api/instructor/nope": {"id": "nope"}})
    assert api.instructor_name("nope") is None
    assert api.instructor_name("nope") is None
    assert api.calls.count("/api/instructor/nope") == 1


def test_instructor_name_of_none_makes_no_request():
    api = api_with_class()
    assert api.instructor_name(None) is None
    assert api.calls == []


def test_instructor_name_builds_a_name_from_first_and_last():
    api = FakeAPI({"/api/instructor/i9": {"id": "i9", "first_name": "Matt", "last_name": "Wilpers"}})
    assert api.instructor_name("i9") == "Matt Wilpers"


# --- regression: issue #10's verified rides --------------------------------

PZ_MAX_SEGMENTS = [
    {"offsets": {"start": 60, "end": 179}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 210, "end": 239}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 270, "end": 299}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 300, "end": 359}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 2, "upper": 2}]},
    {"offsets": {"start": 360, "end": 419}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 3, "upper": 3}]},
    {"offsets": {"start": 420, "end": 479}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 480, "end": 599}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 4, "upper": 4}]},
    {"offsets": {"start": 600, "end": 659}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 660, "end": 719}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 5, "upper": 5}]},
    {"offsets": {"start": 720, "end": 779}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 780, "end": 899}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 5, "upper": 5}]},
    {"offsets": {"start": 900, "end": 1019}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 1020, "end": 1199}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 5, "upper": 5}]},
    {"offsets": {"start": 1200, "end": 1379}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 1380, "end": 1499}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 5, "upper": 5}]},
    {"offsets": {"start": 1500, "end": 1619}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 1620, "end": 1679}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 5, "upper": 5}]},
    {"offsets": {"start": 1680, "end": 1859}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
]

PZE_SEGMENTS = [
    {"offsets": {"start": 60, "end": 179}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 210, "end": 239}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 270, "end": 299}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 330, "end": 359}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 360, "end": 389}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 2, "upper": 2}]},
    {"offsets": {"start": 390, "end": 449}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 3, "upper": 3}]},
    {"offsets": {"start": 450, "end": 479}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 4, "upper": 4}]},
    {"offsets": {"start": 480, "end": 539}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    {"offsets": {"start": 540, "end": 899}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 3, "upper": 3}]},
    {"offsets": {"start": 900, "end": 1019}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 2, "upper": 2}]},
    {"offsets": {"start": 1020, "end": 1379}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 3, "upper": 3}]},
    {"offsets": {"start": 1380, "end": 1499}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 2, "upper": 2}]},
    {"offsets": {"start": 1500, "end": 1769}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 3, "upper": 3}]},
    {"offsets": {"start": 1770, "end": 1859}, "segment_type": "power_zone",
     "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
]


def test_zone_sum_reproduces_the_stored_pz_max_ride():
    # 30 min Power Zone Ride, class f3105dfcd6a0445eab35d50c5b207c80
    payload = {"target_metrics_data": {"target_metrics": PZ_MAX_SEGMENTS}}
    assert peloton_api.sum_target_zones(payload) == {1: 960, 2: 60, 3: 60, 4: 120, 5: 540}


def test_zone_sum_reproduces_the_stored_pze_ride():
    # 30 min Power Zone Endurance Classic Rock, class 456b191339f74c56b576e3cd07c38927
    payload = {"target_metrics_data": {"target_metrics": PZE_SEGMENTS}}
    assert peloton_api.sum_target_zones(payload) == {1: 360, 2: 270, 3: 1050, 4: 30}


# --- CLI -------------------------------------------------------------------

import peloton_class_resolve as cli  # noqa: E402


def test_resolve_all_emits_one_record_per_class_id():
    records = cli.resolve_all(api_with_class(), [CLASS_ID], [], "UTC")
    assert len(records) == 1
    assert records[0]["workout_id"] is None
    assert records[0]["class_id"] == CLASS_ID


def test_resolve_all_labels_records_with_the_workout_that_asked():
    api = api_with_class(**{"/api/workout/w1": {"id": "w1", "ride": {"id": CLASS_ID}}})
    records = cli.resolve_all(api, [], ["w1"], "UTC")
    assert records[0]["workout_id"] == "w1"
    assert records[0]["title"] == "45 min Power Zone Endurance Ride"


def test_resolve_all_fetches_a_shared_class_once():
    api = api_with_class(
        **{
            "/api/workout/w1": {"id": "w1", "ride": {"id": CLASS_ID}},
            "/api/workout/w2": {"id": "w2", "ride": {"id": CLASS_ID}},
        }
    )
    records = cli.resolve_all(api, [], ["w1", "w2"], "UTC")
    assert [r["workout_id"] for r in records] == ["w1", "w2"]
    assert api.calls.count(f"/api/ride/{CLASS_ID}/details") == 1


def test_resolve_all_keeps_a_classless_workout_as_an_explicit_null():
    # "no class" must be distinguishable from "not looked up".
    api = api_with_class(**{"/api/workout/w9": {"id": "w9", "ride": {"id": "0" * 32}}})
    records = cli.resolve_all(api, [], ["w9"], "UTC")
    assert records == [{"workout_id": "w9", "class_id": None}]


def test_render_csv_flattens_zones_into_per_zone_columns():
    records = cli.resolve_all(api_with_class(), [CLASS_ID], [], "UTC")
    rows = cli.render(records, "csv").split("\n")
    assert rows[0].startswith("workout_id,class_id,title,instructor,duration_min")
    assert rows[0].endswith("zone1_sec,zone2_sec,zone3_sec,zone4_sec,zone5_sec,zone6_sec,zone7_sec")
    header = rows[0].split(",")
    values = dict(zip(header, rows[1].split(",")))
    assert values["zone1_sec"] == "300"
    assert values["zone2_sec"] == "2280"
    assert values["zone3_sec"] == ""


def test_render_csv_joins_class_types_without_breaking_the_row():
    records = cli.resolve_all(api_with_class(), [CLASS_ID], [], "UTC")
    body = cli.render(records, "csv").split("\n")[1]
    assert "Power Zone" in body
    assert len(body.split(",")) == len(cli.CSV_COLUMNS)


def test_render_jsonl_is_one_object_per_line():
    records = cli.resolve_all(api_with_class(), [CLASS_ID], [], "UTC")
    lines = cli.render(records, "jsonl").split("\n")
    assert len(lines) == 1
    import json as _json
    assert _json.loads(lines[0])["class_id"] == CLASS_ID


def test_render_json_wraps_records_with_run_metadata():
    import json as _json
    records = cli.resolve_all(api_with_class(), [CLASS_ID], [], "UTC")
    payload = _json.loads(cli.render(records, "json"))
    assert payload["run_metadata"]["total_classes"] == 1
    assert payload["classes"][0]["class_id"] == CLASS_ID


def test_cli_requires_at_least_one_id():
    parser = cli.build_parser()
    args = parser.parse_args([])
    assert args.class_id == [] and args.workout_id == [] and args.stdin is False


def test_cli_accepts_repeated_ids():
    args = cli.build_parser().parse_args(["--class-id", "a", "--class-id", "b", "--workout-id", "w"])
    assert args.class_id == ["a", "b"]
    assert args.workout_id == ["w"]


def test_cli_defaults_the_timezone_to_the_airtable_zone():
    assert cli.build_parser().parse_args(["--class-id", "a"]).timezone == peloton_api.DEFAULT_CLASS_TZ
