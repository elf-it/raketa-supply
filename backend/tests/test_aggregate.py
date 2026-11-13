"""Агрегация по уровням просмотра.

Критерий приёмки № 4: итоги по магазинам, категориям и SKU согласованы между собой,
двойного учёта запасов нет.
"""

from app.calc import SeriesInput, Status, aggregate, compute_series, week_range, worst_status
from app.calc.aggregate import ADDITIVE_FIELDS

WEEKS = week_range("2026-W46", 6)

# (магазин, категория, базовый потенциал, стартовый остаток)
PAIRS = [
    ("Магазин 01", "Лампы", 120, 900),
    ("Магазин 01", "Лампы", 95, 400),
    ("Магазин 01", "Рации", 48, 120),
    ("Магазин 02", "Лампы", 80, 150),
    ("Магазин 02", "Рации", 60, 1000),
    ("Магазин 03", "Аксессуары", 64, 0),
]


def build_all():
    results = {}
    for index, (store, category, potential, stock) in enumerate(PAIRS):
        data = SeriesInput(
            weeks=list(WEEKS),
            opening_stock=float(stock),
            base_potential=float(potential),
            seasonality=dict.fromkeys(WEEKS, 1.1),
        )
        results[index] = (store, category, compute_series(data))
    return results


def test_levels_agree_on_every_additive_metric():
    results = build_all()
    all_series = [r for _, _, r in results.values()]

    by_store: dict[str, list] = {}
    by_category: dict[str, list] = {}
    for store, category, series in results.values():
        by_store.setdefault(store, []).append(series)
        by_category.setdefault(category, []).append(series)

    total = aggregate(all_series, WEEKS).by_week()
    store_level = [aggregate(v, WEEKS).by_week() for v in by_store.values()]
    category_level = [aggregate(v, WEEKS).by_week() for v in by_category.values()]

    for week in WEEKS:
        for field in ADDITIVE_FIELDS:
            expected = getattr(total[week], field)
            from_stores = sum(getattr(m[week], field) for m in store_level)
            from_categories = sum(getattr(m[week], field) for m in category_level)
            assert abs(from_stores - expected) < 1e-9, (week, field)
            assert abs(from_categories - expected) < 1e-9, (week, field)


def test_stock_is_not_counted_twice():
    """Сумма остатков по уровням равна сумме остатков позиций, а не кратна ей."""
    results = build_all()
    all_series = [r for _, _, r in results.values()]
    total = aggregate(all_series, WEEKS).by_week()

    first_week = WEEKS[0]
    expected = sum(s.by_week()[first_week].adapted_stock for s in all_series)
    assert total[first_week].adapted_stock == expected


def test_diff_is_not_aggregated():
    results = build_all()
    aggregated = aggregate([r for _, _, r in results.values()], WEEKS)
    assert all(cell.diff_percent is None for cell in aggregated.cells)


def test_group_status_is_the_worst_one():
    assert worst_status([Status.OK, Status.OVER]) is Status.OVER
    assert worst_status([Status.OK, Status.OVER, Status.SHORT]) is Status.SHORT
    assert worst_status([]) is Status.OK

    results = build_all()
    # В выборке есть позиция с нулевым остатком, значит группа целиком — SHORT.
    aggregated = aggregate([r for _, _, r in results.values()], WEEKS)
    assert aggregated.status is Status.SHORT
