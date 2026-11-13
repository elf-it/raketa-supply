"""Массовые корректировки прогноза (задача C-14).

Проверяется через API на демо-данных: операции трогают именно то, что выбрано,
не ломают закрытые недели и попадают в журнал корректировок.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def scope(client):
    """Две позиции и три плановые недели, с исходными значениями прогноза."""
    payload = client.get("/api/v1/matrix?level=sku").json()
    rows = payload["rows"][:2]
    weeks = [w["key"] for w in payload["weeks"] if not w["is_closed"]][:3]
    closed = [w["key"] for w in payload["weeks"] if w["is_closed"]]
    return {
        "pair_ids": [row["meta"]["pair_id"] for row in rows],
        "weeks": weeks,
        "closed_week": closed[-1] if closed else None,
        "before": {
            row["meta"]["pair_id"]: {w: row["metrics"]["original_forecast"][w] for w in weeks}
            for row in rows
        },
    }


def forecasts(client, pair_ids, weeks):
    rows = client.get("/api/v1/matrix?level=sku").json()["rows"]
    return {
        row["meta"]["pair_id"]: {w: row["metrics"]["original_forecast"][w] for w in weeks}
        for row in rows
        if row["meta"]["pair_id"] in pair_ids
    }


def reset(client, scope):
    client.post(
        "/api/v1/forecast/bulk",
        json={"pair_ids": scope["pair_ids"], "weeks": scope["weeks"], "operation": "clear"},
    )


def test_scale_moves_diff_not_manual_forecast(client, scope):
    """×% крутит DIFF: так правка продолжает следовать за потенциалом и сезонностью."""
    response = client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "scale",
            "value": 150,
            "author": "tester",
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result["updated_cells"] == len(scope["pair_ids"]) * len(scope["weeks"])
    assert result["affected_pairs"] == len(scope["pair_ids"])

    rows = client.get("/api/v1/matrix?level=sku").json()["rows"]
    for row in rows:
        if row["meta"]["pair_id"] not in scope["pair_ids"]:
            continue
        for week in scope["weeks"]:
            assert row["metrics"]["diff_percent"][week] == 150
            # Ячейка не считается ручной: значение по-прежнему вычисляется формулой.
            assert row["metrics"]["is_manual"][week] is False

    reset(client, scope)


def test_scale_is_relative_and_composes(client, scope):
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "scale",
            "value": 200,
        },
    )
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "scale",
            "value": 50,
        },
    )
    rows = client.get("/api/v1/matrix?level=sku").json()["rows"]
    for row in rows:
        if row["meta"]["pair_id"] in scope["pair_ids"]:
            for week in scope["weeks"]:
                assert row["metrics"]["diff_percent"][week] == 100

    reset(client, scope)


def test_delta_pins_manual_forecast(client, scope):
    """+шт нельзя выразить процентом, поэтому результат фиксируется как ручная правка."""
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "delta",
            "value": 25,
        },
    )
    after = forecasts(client, scope["pair_ids"], scope["weeks"])
    for pair_id, weeks in scope["before"].items():
        for week, value in weeks.items():
            assert after[pair_id][week] == value + 25

    rows = client.get("/api/v1/matrix?level=sku").json()["rows"]
    row = next(r for r in rows if r["meta"]["pair_id"] == scope["pair_ids"][0])
    assert row["metrics"]["is_manual"][scope["weeks"][0]] is True

    reset(client, scope)


def test_delta_does_not_push_forecast_below_zero(client, scope):
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "delta",
            "value": -100000,
        },
    )
    after = forecasts(client, scope["pair_ids"], scope["weeks"])
    assert all(value == 0 for weeks in after.values() for value in weeks.values())

    reset(client, scope)


def test_set_and_clear_round_trip(client, scope):
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "set",
            "value": 42,
        },
    )
    after = forecasts(client, scope["pair_ids"], scope["weeks"])
    assert all(value == 42 for weeks in after.values() for value in weeks.values())

    reset(client, scope)
    restored = forecasts(client, scope["pair_ids"], scope["weeks"])
    assert restored == scope["before"]


def test_move_shifts_demand_without_losing_it(client, scope):
    """Перенос обнуляет источники и добавляет их сумму к целевой неделе."""
    payload = client.get("/api/v1/matrix?level=sku").json()
    future = [w["key"] for w in payload["weeks"] if not w["is_closed"]]
    target = future[5]
    sources = scope["weeks"]

    before_target = {
        row["meta"]["pair_id"]: row["metrics"]["original_forecast"][target]
        for row in payload["rows"]
        if row["meta"]["pair_id"] in scope["pair_ids"]
    }
    moved_expected = {pair_id: sum(weeks.values()) for pair_id, weeks in scope["before"].items()}

    result = client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": sources,
            "operation": "move",
            "target_week": target,
        },
    ).json()
    assert result["target_week"] == target
    assert result["moved_units"] == pytest.approx(sum(moved_expected.values()))

    after = forecasts(client, scope["pair_ids"], sources + [target])
    for pair_id in scope["pair_ids"]:
        assert all(after[pair_id][week] == 0 for week in sources)
        assert after[pair_id][target] == before_target[pair_id] + moved_expected[pair_id]

    reset(client, scope)
    client.post(
        "/api/v1/forecast/bulk",
        json={"pair_ids": scope["pair_ids"], "weeks": [target], "operation": "clear"},
    )


def test_closed_weeks_are_skipped(client, scope):
    """План закрытой недели массовая правка не трогает."""
    closed_week = scope["closed_week"]
    assert closed_week, "в демо-данных нет закрытых недель"

    before = client.get("/api/v1/matrix?level=sku").json()["rows"]
    before_value = next(
        r["metrics"]["original_forecast"][closed_week]
        for r in before
        if r["meta"]["pair_id"] == scope["pair_ids"][0]
    )

    result = client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": [closed_week],
            "operation": "set",
            "value": 1,
        },
    ).json()
    assert result["updated_cells"] == 0
    assert result["skipped_closed"] == len(scope["pair_ids"])

    after = client.get("/api/v1/matrix?level=sku").json()["rows"]
    after_value = next(
        r["metrics"]["original_forecast"][closed_week]
        for r in after
        if r["meta"]["pair_id"] == scope["pair_ids"][0]
    )
    assert after_value == before_value


def test_bulk_edits_land_in_the_journal(client, scope):
    """Требование приёмки: корректировки видны в журнале, включая массовые."""
    pair_id = scope["pair_ids"][0]
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "set",
            "value": 123,
            "author": "массовая правка",
        },
    )
    log = client.get(f"/api/v1/forecast/log?pair_id={pair_id}").json()
    entries = [e for e in log if e["author"] == "массовая правка"]
    assert len(entries) >= len(scope["weeks"])
    assert {e["week"] for e in entries} >= set(scope["weeks"])
    assert all(e["new_value"] == 123 for e in entries)

    reset(client, scope)


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"operation": "scale"}, "значение"),
        ({"operation": "scale", "value": 0}, "больше нуля"),
        ({"operation": "set", "value": -5}, "отрицательным"),
        ({"operation": "move"}, "целевая неделя"),
    ],
)
def test_invalid_requests_are_rejected(client, scope, payload, expected):
    response = client.post(
        "/api/v1/forecast/bulk",
        json={"pair_ids": scope["pair_ids"], "weeks": scope["weeks"], **payload},
    )
    assert response.status_code == 400
    assert expected in response.json()["detail"]


def test_move_into_a_selected_week_is_rejected(client, scope):
    response = client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": scope["pair_ids"],
            "weeks": scope["weeks"],
            "operation": "move",
            "target_week": scope["weeks"][0],
        },
    )
    assert response.status_code == 400
    assert "среди переносимых" in response.json()["detail"]
