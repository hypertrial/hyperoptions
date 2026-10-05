from datetime import UTC, date, datetime, timedelta

import pytest

from stocksweeper.forecast.sec_events import (
    parse_actual_results,
    parse_forward_schedule,
    record_actual,
    record_schedule,
    verified_past_earnings_events,
)

ACCEPTED = datetime(2026, 9, 25, 16, tzinfo=UTC)
FUTURE = (
    b"<p>On September 25, 2026, Acme announced that on September 30, 2026 "
    b"it will release its third quarter 2026 financial results.</p>"
)


def parse_actual(content):
    return parse_actual_results(
        "ACME",
        12345,
        "0000012345-26-000002",
        ACCEPTED,
        ACCEPTED + timedelta(minutes=5),
        "ex99-1.htm",
        content,
    )


def test_future_announcement_is_not_an_actual_release():
    actual = parse_actual(FUTURE)
    assert actual is None, "A future announcement is not an actual results release"


def test_false_actual_does_not_verify_the_old_schedule(tmp_path):
    earlier = datetime(2026, 9, 21, 16, tzinfo=UTC)
    schedule = parse_forward_schedule(
        "ACME",
        12345,
        "0000012345-26-000001",
        earlier,
        earlier + timedelta(minutes=5),
        "ex99-1.htm",
        b"Acme will report third quarter 2026 financial results on September 25, 2026.",
    )
    assert schedule is not None
    record_schedule(tmp_path, schedule)
    actual = parse_actual(FUTURE)
    if actual is not None:
        record_actual(tmp_path, actual)
    events = verified_past_earnings_events(tmp_path, "ACME", datetime(2026, 9, 28, 16, tzinfo=UTC))
    assert events == (), "Postponing a future release cannot verify its old scheduled date"


@pytest.mark.parametrize(
    "content, expected",
    [
        (
            b"On September 25, 2026, Acme announced its third quarter 2026 financial results.",
            date(2026, 9, 25),
        ),
        (b"Acme reported results.", None),
        (
            b"On September 25, 2026, Acme announced it will release third quarter 2026 "
            b"financial results on September 30, 2026.",
            None,
        ),
    ],
)
def test_nearby_controls(content, expected):
    actual = parse_actual(content)
    assert (actual.event_date if actual else None) == expected


@pytest.mark.parametrize(
    "content",
    [
        b"On September 25, 2026, Acme announced it will release financial results next week.",
        b"On September 25, 2026, Acme announced it would release financial results next week.",
        b"On September 25, 2026, Acme announced financial results would be released next week.",
        b"On September 25, 2026, Acme announced financial results will be released next week.",
        b"Acme will have reported its financial results on September 25, 2026.",
        b"On September 25, 2026, Acme announced financial results "
        b"are expected to be released next week.",
        b"On September 25, 2026, Acme announced financial results "
        b"are scheduled for release next week.",
        b"Acme announced it plans to release financial results on September 25, 2026.",
        b"On September 25, 2026, Acme announced its earnings release date "
        b"and will report financial results next week.",
        b"On September 25, 2026, Acme announced its quarterly results date "
        b"and will release financial results next week.",
        b"On September 25, 2026, Acme announced financial results; "
        b"it will release them next week.",
        b"On September 25, 2026, Acme announced financial results "
        b"and will host a conference call to release them next week.",
        b"On September 25, 2026, Acme announced its earnings release date.",
        b"On September 25, 2026, Acme announced its earnings release date "
        b"and will host a conference call to discuss financial results next week.",
        b"On September 25, 2026, Acme announced a webcast "
        b"and will host a conference call to discuss upcoming financial results.",
        b"On September 25, 2026, Acme announced a conference call "
        b"and will host a webcast to discuss financial results next week.",
        b"On September 25, 2026, Acme announced a webcast to discuss financial results.",
        b"On September 25, 2026, Acme announced its financial results date "
        b"and will host a conference call next week.",
        b"On September 25, 2026, Acme announced a new product. Financial results follow next week.",
        b"Acme announced a new product. Financial results are due on September 25, 2026.",
    ],
)
def test_future_and_cross_sentence_matches_fail_closed(content):
    assert parse_actual(content) is None


@pytest.mark.parametrize(
    "content",
    [
        b"On Sept. 25, 2026, Acme announced its financial results. "
        b"It will release guidance next week.",
        b"Acme released its financial results on Sept. 25, 2026. "
        b"It will report again next quarter.",
        b"On Sept. 25, 2026, Acme Inc. announced its financial results.",
        b"On September 25, 2026, Acme announced its earnings release.",
        b"On September 25, 2026, Acme reported financial results; "
        b"it will host an earnings conference call to discuss the results.",
        b"On September 25, 2026, Acme reported financial results "
        b"and will host a conference call at 4:30 p.m. ET.",
        b"Acme released financial results on September 25, 2026 "
        b"and will hold a conference call tomorrow.",
    ],
)
def test_actual_clause_survives_unrelated_forward_guidance(content):
    actual = parse_actual(content)
    assert actual is not None
    assert actual.event_date == date(2026, 9, 25)
