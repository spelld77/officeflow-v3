"""Non-blocking input cautions, separate from domain validity and persistence."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class DateInput:
    name: str
    before: date
    after: date
    is_new: bool = False
    far_check: bool = True


@dataclass(frozen=True, slots=True)
class InputNotice:
    text: str
    confirm: bool = False


def review_dates(values: tuple[DateInput, ...], *, today: date) -> tuple[InputNotice, ...]:
    def display(day: date) -> str:
        return f"{day:%Y-%m-%d}({('월', '화', '수', '목', '금', '토', '일')[day.weekday()]})"

    notices = []
    for value in values:
        before, after = value.before, value.after
        changed = before != after
        year_changed = before.year != after.year
        far = value.is_new and value.far_check and abs((after - today).days) > 366
        risky = changed and (
            (year_changed and (before.month, before.day) == (after.month, after.day))
            or abs((after - before).days) >= 180
            or far
        )
        if changed and year_changed:
            notices.append(
                InputNotice(f"{value.name} 연도 변경: {display(before)} → {display(after)}", risky)
            )
        elif far:
            direction = "전" if after < today else "후"
            notices.append(
                InputNotice(
                    f"{value.name}: {after:%Y-%m-%d} · 현재 기준 1년 이상 {direction}", risky
                )
            )
        elif risky:
            notices.append(
                InputNotice(f"{value.name} 날짜 변경: {display(before)} → {display(after)}", True)
            )
    return tuple(notices)
