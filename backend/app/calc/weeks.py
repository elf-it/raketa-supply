"""Работа с ISO-неделями.

Неделя всюду в системе — строка вида ``2026-W46``. Такой ключ лексикографически
сортируется в хронологическом порядке, однозначно разворачивается в понедельник
и переносится в JSON без часовых поясов.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def week_key(day: date) -> str:
    """ISO-ключ недели, в которую попадает дата."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def parse_week(key: str) -> tuple[int, int]:
    m = WEEK_RE.match(key)
    if not m:
        raise ValueError(f"некорректный ключ недели: {key!r}, ожидается YYYY-Www")
    return int(m.group(1)), int(m.group(2))


def week_start(key: str) -> date:
    """Понедельник недели."""
    year, week = parse_week(key)
    return date.fromisocalendar(year, week, 1)


def week_end(key: str) -> date:
    """Воскресенье недели."""
    return week_start(key) + timedelta(days=6)


def week_number(key: str) -> int:
    """Номер недели в году, 1..53 — ключ для коэффициентов сезонности."""
    return parse_week(key)[1]


def shift_week(key: str, weeks: int) -> str:
    return week_key(week_start(key) + timedelta(weeks=weeks))


def week_range(start: str, count: int) -> list[str]:
    """``count`` недель подряд, начиная со ``start`` включительно."""
    if count < 0:
        raise ValueError("count не может быть отрицательным")
    return [shift_week(start, i) for i in range(count)]


def weeks_between(start: str, end: str) -> list[str]:
    """Все недели от ``start`` до ``end`` включительно."""
    if start > end:
        return []
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur = shift_week(cur, 1)
    return out


def iso_weeks_in_year(year: int) -> int:
    """52 или 53 — сколько ISO-недель в году."""
    return date(year, 12, 28).isocalendar()[1]
