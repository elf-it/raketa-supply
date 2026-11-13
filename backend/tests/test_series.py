"""Прогноз → остатки. Формулы — docs/03-calc-spec.md."""

from app.calc import CalcSettings, SeriesInput, Status, compute_series, week_range

WEEKS = week_range("2026-W46", 4)


def make_input(**kwargs) -> SeriesInput:
    base = {
        "weeks": list(WEEKS),
        "opening_stock": 660.0,
        "base_potential": 100.0,
        "seasonality": {"2026-W46": 1.10, "2026-W47": 1.15, "2026-W48": 1.20, "2026-W49": 1.25},
    }
    base.update(kwargs)
    return SeriesInput(**base)


def test_original_forecast_is_potential_times_seasonality():
    result = compute_series(make_input())
    assert [c.original_forecast for c in result.cells] == [110, 115, 120, 125]


def test_real_stock_follows_original_demand_from_mockup():
    """Числа с макета, стр. 3 ТЗ: 660 - 110/115/120/125 → 550, 435, 315, 190."""
    result = compute_series(make_input())
    assert [c.real_stock for c in result.cells] == [550, 435, 315, 190]


def test_adapted_stock_follows_manual_adapted_forecast():
    """Та же строка макета: ручная правка адаптированного прогноза W49 = 70 → сток 245."""
    result = compute_series(make_input(manual_adapted={"2026-W49": 70}))
    assert [c.adapted_forecast for c in result.cells] == [110, 115, 120, 70]
    assert [c.adapted_stock for c in result.cells] == [550, 435, 315, 245]


def test_diff_percent_scales_forecast():
    result = compute_series(make_input(diff_percent={"2026-W48": 50}))
    cells = result.by_week()
    assert cells["2026-W48"].original_forecast == 60  # round(100 * 1.20 * 0.5)
    assert cells["2026-W48"].diff_percent == 50
    assert cells["2026-W47"].diff_percent == 100  # по умолчанию DIFF не меняет прогноз


def test_manual_forecast_wins_over_formula():
    """Критерий приёмки № 2: ручная правка переживает пересчёт."""
    result = compute_series(make_input(manual_forecast={"2026-W48": 999}))
    cells = result.by_week()
    assert cells["2026-W48"].original_forecast == 999
    assert cells["2026-W48"].is_manual is True


def test_shortage_keeps_original_demand():
    """Ключевой принцип ТЗ: дефицит обрезает адаптированный прогноз, но не исходный."""
    result = compute_series(make_input(opening_stock=150.0))
    cells = result.cells

    assert [c.original_forecast for c in cells] == [110, 115, 120, 125]
    assert [c.adapted_forecast for c in cells] == [110, 40, 0, 0]
    assert [c.adapted_stock for c in cells] == [40, 0, 0, 0]
    # Реальный сток уходит в минус — это и есть неудовлетворённый спрос.
    assert [c.real_stock for c in cells] == [40, -75, -195, -320]
    assert [c.shortage_units for c in cells] == [0, 75, 120, 125]
    assert [c.stock_limited for c in cells] == [False, True, True, True]
    assert result.status is Status.SHORT


def test_supply_restores_sales():
    result = compute_series(make_input(opening_stock=150.0, supply={"2026-W48": 500}))
    cells = result.by_week()
    assert cells["2026-W48"].adapted_forecast == 120
    assert cells["2026-W48"].adapted_stock == 380
    assert cells["2026-W49"].adapted_forecast == 125


def test_over_status_on_excess_coverage():
    data = make_input(
        opening_stock=0.0,
        last_closed_week="2026-W46",
        fact_sales={"2026-W46": 100},
        fact_stock_end={"2026-W46": 5000},
    )
    result = compute_series(data, CalcSettings(over_coverage_weeks=12))
    assert result.coverage_weeks is not None
    assert result.coverage_weeks > 12
    assert result.status is Status.OVER


def test_closed_week_uses_fact_and_frozen_plan():
    data = make_input(
        last_closed_week="2026-W46",
        plan={"2026-W46": 200},
        fact_sales={"2026-W46": 180},
        fact_stock_end={"2026-W46": 400},
    )
    result = compute_series(data)
    closed = result.cells[0]

    assert closed.is_closed is True
    assert closed.original_forecast == 200  # зафиксированный план, формула не пересчитывает
    assert closed.adapted_forecast == 180  # факт продаж
    assert closed.real_stock == 400  # физический остаток из WB — источник истины
    assert closed.diff_percent is None  # на закрытой неделе DIFF не редактируется
    # Плановые недели стартуют от фактического остатка закрытой недели.
    assert result.cells[1].real_stock == 400 - 115
