"""Tests for accepting class URLs and emitting the ordered segment plan.

Closes the gap left by issue #4: the resolver had the data but took only a bare
class id, and reported per-zone totals without the segment sequence that shows
the shape of the ride.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import peloton_api  # noqa: E402
import peloton_class_resolve as cli  # noqa: E402

CLASS_ID = "992fad43102b4117b36caeea1e0f077d"
WORKOUT_ID = "89261a636dc94fc6b0db0ca1b006e65f"


# --- class URLs ------------------------------------------------------------

def test_extracts_the_canonical_minimal_class_url():
    url = f"https://members.onepeloton.com/classes/cycling?modal=classDetailsModal&classId={CLASS_ID}"
    assert cli.class_id_from_text(url) == CLASS_ID


def test_ignores_category_slug_and_the_account_tied_share_token():
    # Real share links carry extra params; only classId is meaningful, and
    # `code` is tied to the account that generated it.
    url = (f"https://members.onepeloton.com/classes/cycling?categorySlug=power-zone"
           f"&modal=classDetailsModal&classId={CLASS_ID}&code=abc123def")
    assert cli.class_id_from_text(url) == CLASS_ID


def test_accepts_a_class_url_in_any_param_order():
    url = f"https://members.onepeloton.com/classes/cycling?classId={CLASS_ID}&modal=classDetailsModal"
    assert cli.class_id_from_text(url) == CLASS_ID


def test_accepts_a_bare_class_id_unchanged():
    assert cli.class_id_from_text(CLASS_ID) == CLASS_ID
    assert cli.class_id_from_text(f"  {CLASS_ID}  ") == CLASS_ID


def test_rejects_a_workout_url_as_a_class_id():
    # A workout URL is a real input, but it belongs to --workout-id. Silently
    # treating its id as a class id would resolve the wrong thing.
    url = f"https://members.onepeloton.com/profile/workouts/{WORKOUT_ID}"
    try:
        cli.class_id_from_text(url)
    except ValueError as exc:
        assert "workout" in str(exc).lower()
        return
    raise AssertionError("expected ValueError naming the workout URL")


def test_rejects_a_url_with_no_class_id():
    try:
        cli.class_id_from_text("https://members.onepeloton.com/classes/cycling")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_rejects_a_malformed_id():
    for bad in ("", "not-an-id", "1234", CLASS_ID[:-1], CLASS_ID + "ff"):
        try:
            cli.class_id_from_text(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_workout_id_from_text_reads_a_workout_url():
    url = f"https://members.onepeloton.com/profile/workouts/{WORKOUT_ID}"
    assert cli.workout_id_from_text(url) == WORKOUT_ID


def test_workout_id_from_text_accepts_a_bare_id():
    assert cli.workout_id_from_text(WORKOUT_ID) == WORKOUT_ID


def test_workout_id_from_text_rejects_a_class_url():
    url = f"https://members.onepeloton.com/classes/cycling?classId={CLASS_ID}"
    try:
        cli.workout_id_from_text(url)
    except ValueError as exc:
        assert "class" in str(exc).lower()
        return
    raise AssertionError("expected ValueError naming the class URL")


# --- segments --------------------------------------------------------------

DETAILS = {
    "ride": {
        "id": CLASS_ID, "title": "60 min Power Zone Ride", "duration": 3600,
        "fitness_discipline": "cycling", "scheduled_start_time": 1749211200,
        "instructor": {"id": "i1", "name": "Matt Wilpers"},
    },
    "is_power_zone_class": True,
    "is_ftp_test": False,
    "class_types": [{"name": "Power Zone"}],
    "target_metrics_data": {"target_metrics": [
        {"offsets": {"start": 60, "end": 359}, "segment_type": "power_zone",
         "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
        {"offsets": {"start": 360, "end": 659}, "segment_type": "power_zone",
         "metrics": [{"name": "power_zone", "lower": 3, "upper": 3}]},
        {"offsets": {"start": 660, "end": 719}, "segment_type": "power_zone",
         "metrics": [{"name": "power_zone", "lower": 1, "upper": 1}]},
    ]},
}


def test_target_segments_preserves_order_and_inclusive_duration():
    segments = peloton_api.target_segments(DETAILS)
    assert segments == [
        {"zone": 1, "start_sec": 60, "end_sec": 359, "duration_sec": 300},
        {"zone": 3, "start_sec": 360, "end_sec": 659, "duration_sec": 300},
        {"zone": 1, "start_sec": 660, "end_sec": 719, "duration_sec": 60},
    ]


def test_target_segments_does_not_merge_repeated_zones():
    # Zone 1 appears twice; the totals collapse it, the sequence must not.
    zones = [s["zone"] for s in peloton_api.target_segments(DETAILS)]
    assert zones == [1, 3, 1]


def test_target_segments_agrees_with_the_zone_totals():
    segments = peloton_api.target_segments(DETAILS)
    rolled = {}
    for s in segments:
        rolled[s["zone"]] = rolled.get(s["zone"], 0) + s["duration_sec"]
    assert rolled == peloton_api.sum_target_zones(DETAILS)


def test_target_segments_on_a_class_with_no_plan():
    assert peloton_api.target_segments({}) == []
    assert peloton_api.target_segments({"target_metrics_data": {"target_metrics": []}}) == []


def test_target_segments_skips_broken_offsets():
    payload = {"target_metrics_data": {"target_metrics": [
        {"offsets": {"start": 10, "end": 5}, "metrics": [{"name": "power_zone", "lower": 2}]},
        {"offsets": {"start": 0, "end": 9}, "metrics": [{"name": "power_zone", "lower": 2}]},
    ]}}
    assert [s["start_sec"] for s in peloton_api.target_segments(payload)] == [0]


def test_resolve_class_includes_the_segment_plan():
    class FakeAPI(peloton_api.PelotonAPI):
        def __init__(self):
            super().__init__("tok", page_pause=0)

        def _get(self, path, params=None):
            return DETAILS

    record = FakeAPI().resolve_class(CLASS_ID)
    assert record["segments"][0] == {"zone": 1, "start_sec": 60, "end_sec": 359, "duration_sec": 300}
    assert len(record["segments"]) == 3
    assert record["zones"] == {1: 360, 3: 300}


def test_csv_reports_the_segment_count_not_the_whole_plan():
    # The plan is a list; a CSV cell is not the place for it.
    record = {"class_id": CLASS_ID, "zones": {1: 360},
              "segments": [{"zone": 1, "start_sec": 0, "end_sec": 359, "duration_sec": 360}]}
    row = cli.flatten_for_csv(record)
    assert row["segment_count"] == 1
    assert "segments" not in row
