"""Peloton DOM selector inventory — single source of truth.

Peloton uses `data-test-id` (hyphenated) on most interactive elements.
Preference order: data-test-id > ARIA > text > CSS class.
"""

SELECTORS = {
    # Workout page
    "workout_info_block": '[data-test-id="workoutBasicInfoBlock"]',
    "ride_title": '[data-test-id="classTitle"]',
    "class_info_block": '[data-test-id="classBasicInfoBlock"]',
    "instructor_photo": '[data-test-id="instructorPhoto"]',
    "view_class_button": '[data-test-id="viewClassButton"]',
    "delete_workout": '[data-test-id="deleteWorkout"]',

    # Class details modal
    "class_details_modal": '[data-test-id="classDetailsModal"]',
    "modal_title": '[data-test-id="modalTitle"]',
    "modal_close": '[data-test-id="closeModalButton"]',
    "view_details_button": 'button:has-text("View Details")',

    # Class plan segments
    "segment_container": '[data-test-id="segmentContainer"]',
    "segment_name": '[data-test-id="segmentName"]',
    "segment_duration": '[data-test-id="segmentDuration"]',

    # Class plan subsegments (movements)
    "subsegment_name": '[data-test-id="subsegmentName"]',
    "subsegment_duration": '[data-test-id="subsegmentDuration"]',

    # Auth / login
    "login_email": 'input[name="usernameOrEmail"]',
    "login_password": 'input[name="password"]',
    "login_submit": 'button[type="submit"]',

    # Cookie banner
    "cookie_dismiss": 'button:has-text("Acknowledge")',
}
