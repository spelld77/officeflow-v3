from datetime import date

import pytest

from officeflow.application.input_review import DateInput, review_dates


@pytest.mark.parametrize(
    ("before", "after", "confirm"),
    [
        (date(2026, 10, 15), date(2025, 10, 15), True),
        (date(2026, 12, 31), date(2027, 1, 1), False),
        (date(2026, 10, 15), date(2027, 6, 1), True),
        (date(2028, 2, 29), date(2028, 2, 29), False),
    ],
)
def test_year_changes_are_reviewed_without_blocking_valid_dates(before, after, confirm):
    notices = review_dates((DateInput("시작일", before, after),), today=date(2026, 10, 15))
    assert any(item.confirm for item in notices) is confirm
    if before == after:
        assert not notices
    else:
        assert any(str(after.year) in item.text for item in notices)


def test_explicit_initial_calendar_date_is_not_a_dangerous_edit():
    day = date(2029, 1, 1)
    notices = review_dates((DateInput("시작일", day, day, is_new=True),), today=date(2026, 1, 1))
    assert notices and not any(item.confirm for item in notices)


def test_far_date_and_recurrence_end_have_separate_rules():
    today = date(2026, 1, 1)
    notices = review_dates(
        (
            DateInput("새 날짜", today, date(2028, 1, 2), is_new=True),
            DateInput(
                "반복 종료", date(2030, 1, 1), date(2030, 1, 1), is_new=True, far_check=False
            ),
        ),
        today=today,
    )
    assert len(notices) == 1 and notices[0].confirm
