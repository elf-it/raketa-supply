"""Расчёт закупки. Формулы — docs/03-calc-spec.md, раздел 5."""

from datetime import date, timedelta

from pytest import approx

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


def test_breakdown_reconstructs_quantity():
    """Экран объясняет количество этими числами, поэтому они обязаны сходиться."""
    params = PurchaseParams(
        lead_time_days=7, coverage_weeks=4, safety_weeks=2.0, moq=0, pack_size=1
    )
    result = suggest_purchase(
        WEEKS, DEMAND, {"2026-W49": 150.0}, opening_stock=450.0, params=params, today=TODAY
    )
    assert result.coverage_from == result.shortage_week
    assert result.coverage_weeks_used == 4
    assert result.window_transit == 150.0
    assert (
        result.window_demand + result.safety_stock - result.stock_before - result.window_transit
        == result.qty_before_constraints
    )


def test_coverage_window_is_clipped_by_horizon():
    """В конце горизонта окно покрытия короче заданного — экран показывает фактическое."""
    weeks = week_range("2026-W46", 3)
    params = PurchaseParams(coverage_weeks=8, safety_weeks=0.0, moq=0, pack_size=1)
    result = suggest_purchase(
        weeks,
        dict.fromkeys(weeks, 100.0),
        {},
        opening_stock=0.0,
        params=params,
        today=date(2026, 10, 20),  # заказ ещё не просрочен, окно начинается с дефицита
    )
    assert result.coverage_weeks_used == 3
    assert result.coverage_to == weeks[-1]


def test_overdue_window_starts_at_the_arrival_week():
    """Просроченный заказ не покрывает недели, которые пройдут до прихода товара."""
    params = PurchaseParams(
        lead_time_days=60, coverage_weeks=4, safety_weeks=0.0, moq=0, pack_size=1
    )
    result = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=50.0, params=params, today=TODAY)
    assert result.is_overdue is True
    assert result.coverage_from == result.arrival_week > result.shortage_week
    # Минус реального стока — потерянные продажи, а не долг: в заказ он не попадает.
    assert result.stock_before == 0.0
    assert result.qty_before_constraints == 4 * 100.0


def test_order_already_placed_removes_the_need():
    """Повторный расчёт после заказа не предлагает купить то же самое снова."""
    params = PurchaseParams(
        lead_time_days=60, coverage_weeks=4, safety_weeks=0.0, moq=0, pack_size=1
    )
    first = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=50.0, params=params, today=TODAY)
    assert first.arrival_week is not None
    # Разместили ровно то, что посоветовал расчёт: партия придёт на неделе прихода.
    again = suggest_purchase(
        WEEKS,
        DEMAND,
        {first.arrival_week: float(first.qty)},
        opening_stock=50.0,
        params=params,
        today=TODAY,
    )
    assert again.qty == 0


def test_arrival_beyond_horizon_is_not_an_order():
    weeks = week_range("2026-W46", 3)
    params = PurchaseParams(lead_time_days=90, coverage_weeks=4, safety_weeks=0.0)
    result = suggest_purchase(
        weeks, dict.fromkeys(weeks, 100.0), {}, opening_stock=0.0, params=params, today=TODAY
    )
    assert result.needed is False
    assert "за горизонтом" in result.reason


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


# ------------------------------------------------------------------------- ROI


def test_roi_turns_margin_and_turnover_into_percent_per_year():
    """Доходность вложенных денег: маржа, приведённая к году циклом заказа.

    Себестоимость 100, цена 180 — маржа 80 %. Спрос 100 шт./нед., заказ на окно
    в 4 недели без страхового запаса и без остатка — 400 шт., значит
    распродастся за 4 недели. Срок поставки 14 дней добавляет две недели:
    деньги заняты 6 недель, то есть 52 / 6 оборота в год.
    """
    params = PurchaseParams(
        lead_time_days=14,
        coverage_weeks=4,
        safety_weeks=0.0,
        moq=0,
        pack_size=1,
        cost_price=100.0,
        price=180.0,
    )

    result = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=0.0, params=params, today=TODAY)

    assert result.qty == 400
    assert result.sell_out_weeks == approx(4.0)
    assert result.cycle_weeks == approx(6.0)
    assert result.roi_percent == approx(0.8 * 52 / 6 * 100, rel=1e-6)


def test_fast_position_with_small_margin_beats_slow_one_with_big_margin():
    """Ради этого ROI и приводится к году: иначе сравнить их нечем.

    У первой позиции маржа 30 % и оборот — месяц, у второй 80 % и полгода.
    Денег хватает не на всё, и выбирать нужно первую.
    """
    fast = PurchaseParams(
        lead_time_days=7, coverage_weeks=2, safety_weeks=0.0, cost_price=100.0, price=130.0
    )
    slow = PurchaseParams(
        lead_time_days=7, coverage_weeks=20, safety_weeks=0.0, cost_price=100.0, price=180.0
    )

    quick = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=0.0, params=fast, today=TODAY)
    long = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=0.0, params=slow, today=TODAY)

    assert quick.roi_percent > long.roi_percent
    assert quick.sell_out_weeks < long.sell_out_weeks


def test_roi_is_empty_when_there_is_nothing_to_count_from():
    """Пусто — не ноль: ноль означал бы «вложение не окупается»."""
    no_price = PurchaseParams(coverage_weeks=4, safety_weeks=0.0, cost_price=100.0, price=0.0)
    no_cost = PurchaseParams(coverage_weeks=4, safety_weeks=0.0, cost_price=0.0, price=180.0)

    for params in (no_price, no_cost):
        result = suggest_purchase(WEEKS, DEMAND, {}, opening_stock=0.0, params=params, today=TODAY)
        assert result.qty > 0
        assert result.roi_percent is None

    # Спроса нет — заказывать нечего, и считать доходность не из чего.
    quiet = suggest_purchase(
        WEEKS,
        dict.fromkeys(WEEKS, 0.0),
        {},
        opening_stock=0.0,
        params=PurchaseParams(cost_price=100.0, price=180.0),
        today=TODAY,
    )
    assert quiet.qty == 0
    assert quiet.roi_percent is None


def test_roi_does_not_change_what_is_ordered():
    """ROI — показатель, а не правило: на количество и дату он не влияет."""
    base = dict(lead_time_days=14, coverage_weeks=4, safety_weeks=1.0, cost_price=100.0)
    cheap = suggest_purchase(
        WEEKS,
        DEMAND,
        {},
        opening_stock=100.0,
        params=PurchaseParams(**base, price=110.0),
        today=TODAY,
    )
    rich = suggest_purchase(
        WEEKS,
        DEMAND,
        {},
        opening_stock=100.0,
        params=PurchaseParams(**base, price=900.0),
        today=TODAY,
    )

    assert cheap.qty == rich.qty
    assert cheap.order_date == rich.order_date
    assert cheap.arrival_week == rich.arrival_week
    assert rich.roi_percent > cheap.roi_percent
