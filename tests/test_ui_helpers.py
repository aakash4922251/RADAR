from datetime import date, datetime, timezone

from radar_ui import (
    acquisition_status_label,
    buyer_display_name,
    contract_status_label,
    evidence_label,
    format_opportunity_window,
    greeting_for_hour,
    history_timeline_years,
    human_age,
    opportunity_buckets,
    timestamp_is_recent,
)
from ui_theme import environment_for_hour


def test_greeting_changes_with_local_hour():
    assert greeting_for_hour(8, "Asha") == "Good morning, Asha"
    assert greeting_for_hour(14, "Asha") == "Good afternoon, Asha"
    assert greeting_for_hour(20, "Asha") == "Good evening, Asha"


def test_opportunity_window_uses_friendly_month_names():
    assert format_opportunity_window("2026-10-01", "2026-12-31") == "Oct-Dec 2026"
    assert format_opportunity_window("2026-10-01", "2026-10-31") == "Oct 2026"
    assert format_opportunity_window(None, None) == "Expected timing is still being worked out"


def test_evidence_and_status_labels_avoid_internal_enums():
    assert evidence_label("HIGH") == "Strong evidence"
    assert evidence_label("MEDIUM") == "Growing evidence"
    assert evidence_label("LOW") == "Early signal"
    assert contract_status_label("CONFLICTING_EVIDENCE") == "We found conflicting information"
    assert contract_status_label("INSUFFICIENT_EVIDENCE") == "Not enough information yet"
    assert acquisition_status_label("MANUAL_ACTION_REQUIRED") == "Needs a manual check"
    assert buyer_display_name("Ministry||Surveillance Division") == "Ministry · Surveillance Division"
    assert buyer_display_name(None) == "Organisation not listed"


def test_opportunity_filters_are_based_on_real_window_and_observation_dates():
    item = {
        "predicted_date": "2026-10-20",
        "window_start": "2026-10-01",
        "window_end": "2026-11-20",
        "latest_tender_date": "2026-09-25",
    }
    buckets = opportunity_buckets(item, date(2026, 10, 2))
    assert buckets == {"All", "New", "Coming Soon"}

    older = dict(item, window_start="2027-02-01", window_end="2027-03-01", latest_tender_date="2025-01-01")
    assert opportunity_buckets(older, date(2026, 10, 2)) == {"All", "Watching"}


def test_scan_recency_uses_plain_language():
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    assert human_age("2026-10-02T10:30:00+00:00", now) == "Checked 1 hr ago"
    assert human_age(None, now) == "No scan recorded yet"


def test_recent_scan_notification_is_time_limited():
    now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
    assert timestamp_is_recent("2026-10-02T11:00:00+00:00", now=now)
    assert not timestamp_is_recent("2026-09-30T12:00:00+00:00", now=now)
    assert not timestamp_is_recent(None, now=now)


def test_environment_colors_change_by_time_without_darkening_text_surface():
    periods = {environment_for_hour(hour)[0] for hour in (8, 14, 19, 23)}
    assert periods == {"morning", "afternoon", "evening", "night"}
    assert all(environment_for_hour(hour)[1] for hour in (8, 14, 19, 23))


def test_history_timeline_uses_at_most_four_past_years_plus_now():
    dates = [f"{year}-10-01" for year in range(2015, 2026)]
    assert history_timeline_years(dates, current_year=2026) == [
        "2022", "2023", "2024", "2025", "2026"
    ]