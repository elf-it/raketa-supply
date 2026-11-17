"""Постраничная выдача закупки (задача G-17).

Матрица отдаёт строки окнами (G-10), а закупка — нет: нагрузочный замер (G-6)
показал 10,2 МБ на 10 800 позициях. Здесь проверяется, что окно режет только
строки: итоги — штуки, деньги, объём и число просроченных — остаются по всей
выборке. Иначе сумма под таблицей расходилась бы с тем, что над ней, а заказ
поставщику собирают именно по этой сумме.
"""

from __future__ import annotations

PURCHASE = "/api/v1/purchase"


def _plan(client, query: str = "") -> dict:
    answer = client.get(f"{PURCHASE}?{query}")
    assert answer.status_code == 200, answer.text
    return answer.json()


def _ids(plan: dict) -> list[int]:
    return [row["pair_id"] for row in plan["rows"]]


def test_windows_add_up_to_the_whole_selection(client):
    full = _plan(client)
    assert len(full["rows"]) > 6, "демо-каталог слишком мал, чтобы проверить страницы"

    collected: list[int] = []
    offset = 0
    while True:
        page = _plan(client, f"offset={offset}&limit=5")
        if not page["rows"]:
            break
        assert len(page["rows"]) <= 5
        collected += _ids(page)
        offset += 5

    assert collected == _ids(full), "ни пропуска, ни дубля, и порядок тот же"


def test_totals_ignore_the_window(client):
    """Итоги — по выборке, а не по странице: по ним собирают заказ."""
    full = _plan(client)
    page = _plan(client, "offset=2&limit=3")

    assert len(page["rows"]) == 3
    assert page["total_qty"] == full["total_qty"]
    assert page["total_cost"] == full["total_cost"]
    assert page["total_volume_m3"] == full["total_volume_m3"]
    assert page["overdue_count"] == full["overdue_count"]
    assert page["row_count"] == len(full["rows"])
    assert page["offset"] == 2


def test_without_a_window_everything_comes_as_before(client):
    """Выгрузка и прежние клиенты опираются на ответ целиком."""
    full = _plan(client)

    assert full["offset"] == 0
    assert full["row_count"] == len(full["rows"])


def test_a_window_past_the_end_is_empty_but_honest(client):
    full = _plan(client)
    page = _plan(client, f"offset={len(full['rows']) + 10}&limit=5")

    assert page["rows"] == []
    assert (
        page["row_count"] == full["row_count"]
    ), "выборка не изменилась от того, что её пролистали"
    assert page["total_qty"] == full["total_qty"]


def test_the_window_counts_rows_of_the_filtered_selection(client):
    """С отбором «только нужные» окно считает строки того же отбора."""
    needed = _plan(client, "only_needed=true")
    page = _plan(client, "only_needed=true&limit=2")

    assert page["row_count"] == len(needed["rows"])
    assert page["total_qty"] == needed["total_qty"]
    assert _ids(page) == _ids(needed)[:2]


def test_the_export_knows_no_window(client):
    """В файл уходит вся выборка: окно — про экран, а не про выгрузку."""
    answer = client.get(f"{PURCHASE}/export?offset=2&limit=1")

    assert answer.status_code == 200
    assert len(answer.content) > 5000, "файл на одну строку был бы заметно меньше"
