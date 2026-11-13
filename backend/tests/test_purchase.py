"""Расчёт закупки. Формулы — docs/03-calc-spec.md, раздел 5."""

from datetime import date, timedelta

from app.calc import (
    CalcSettings,
    PurchaseParams,
    apply_constraints,
    projected_stock,
    safety_stock,
    suggest_purchase,
    week_range,
    week_start,
)

WEEKS = week_range("2026-W46", 12)
DEMAND = dict.fromkeys(WEEKS, 100.0)
TODAY = date(2026, 11, 2)


def test_safety_stock_is_weeks_of_average_demand():
    params = PurchaseParams(safety_weeks=2.0)
    assert safety_stock(WEEKS, DEMAND, params, CalcSettings(demand_avg_weeks=4)) == 200.0


def test_projected_stock_counts_only_ordered_batches():
    projected = projected_stock(WEEKS, DEMAND, {"2026-W48": 300.0}, opening_stock=250.0)
    assert projected["2026-W46"] == 150.0
    assert projected["2026-W47"] == 50.0
    assert projected["2026-W48"] == 250.0  # пришла партия 300
    assert projected["2026-W49"] == 150.0


def test_no_purchase_when_stock_covers_horizon():
    result = suggest_purchase(
        WEEKS, DEMAND, {}, opening_stock=5000.0, params=PurchaseParams(), today=TODAY
    )
    assert result.needed is False
    assert result.qty == 0


def test_order_date_is_lead_time_before_shortage_week():
    params = PurchaseParams(
        lead_time_days=14, coverage_weeks=4, safety_weeks=2.0, moq=0, pack_size=1
    )
    # Остаток 450, safety = 200 → ниже safety на третьей неделе.
    result = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=450.0, params=params, today=TODAY)
    assert result.shortage_week == "2026-W48"
    assert result.order_date == week_start("2026-W48") - timedelta(days=14)
    assert result.is_overdue is False


def test_quantity_covers_window_plus_safety_minus_stock():
    params = PurchaseParams(
        lead_time_days=7, coverage_weeks=4, safety_weeks=2.0, moq=0, pack_size=1
    )
    result = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=450.0, params=params, today=TODAY)
    # Дефицит на W48, остаток перед ним 250, окно 4 недели по 100, safety 200.
    assert result.qty_before_constraints == 400 + 200 - 250
    assert result.qty == 350


def test_in_transit_reduces_quantity():
    params = PurchaseParams(
        lead_time_days=7, coverage_weeks=4, safety_weeks=2.0, moq=0, pack_size=1
    )
    plain = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=450.0, params=params, today=TODAY)
    with_transit = suggest_purchase(
        WEEKS, DEMAND, {"2026-W49": 150.0}, opening_stock=450.0, params=params, today=TODAY
    )
    assert with_transit.qty == plain.qty - 150


def test_pack_size_and_moq():
    assert apply_constraints(0, PurchaseParams(moq=100, pack_size=50)) == 0
    assert apply_constraints(120, PurchaseParams(moq=0, pack_size=50)) == 150
    assert apply_constraints(30, PurchaseParams(moq=100, pack_size=1)) == 100
    # MOQ тоже округляется до кратности упаковки.
    assert apply_constraints(30, PurchaseParams(moq=110, pack_size=50)) == 150


def test_overdue_order_is_flagged_and_arrival_shifts():
    params = PurchaseParams(lead_time_days=60, coverage_weeks=4, safety_weeks=2.0)
    result = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=250.0, params=params, today=TODAY)
    assert result.is_overdue is True
    # Приход считается от сегодняшней даты, а не от просроченной плановой.
    assert result.arrival_week is not None
    assert result.arrival_week > "2026-W46"


def test_cost_and_volume_follow_quantity():
    params = PurchaseParams(
        lead_time_days=7,
        coverage_weeks=4,
        safety_weeks=0.0,
        moq=0,
        pack_size=1,
        cost_price=10.0,
        volume_m3=0.002,
    )
    result = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=50.0, params=params, today=TODAY)
    assert result.qty > 0
    assert result.cost == result.qty * 10.0
    assert abs(result.volume_m3 - result.qty * 0.002) < 1e-9
