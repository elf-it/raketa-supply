"""Сборка недельной матрицы: БД → расчётное ядро → строки для интерфейса."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..calc import (
    CalcSettings,
    Level,
    SeriesInput,
    SeriesResult,
    Status,
    aggregate,
    compute_series,
    shift_week,
    week_key,
    week_range,
    week_start,
)
from ..models import (
    AppSettings,
    Category,
    ForecastAdjustment,
    Product,
    Seasonality,
    Store,
    StoreProduct,
    Supply,
    SupplyStatus,
    WeekFact,
    WeekPlan,
)

MONTHS_RU = [
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]

#: Показатели, возвращаемые в каждой строке матрицы.
METRICS = (
    "original_forecast",
    "adapted_forecast",
    "real_stock",
    "adapted_stock",
    "fact_sales",
    "supply",
    "diff_percent",
    "shortage_units",
)


@dataclass
class Pair:
    """Пара (магазин, товар) со всем, что нужно для расчёта и отображения."""

    id: int
    store_id: int
    store_name: str
    product_id: int
    vendor_code: str
    title: str
    nm_id: str | None
    category_id: int | None
    category_name: str | None
    niche: str | None
    abc_class: str | None
    base_potential: float


@dataclass
class MatrixFilters:
    store_id: int | None = None
    category_id: int | None = None
    search: str | None = None
    product_id: int | None = None


@dataclass
class Row:
    key: str
    title: str
    subtitle: str | None = None
    status: Status = Status.OK
    meta: dict = field(default_factory=dict)
    metrics: dict[str, dict[str, float | None]] = field(default_factory=dict)


@dataclass
class Matrix:
    level: Level
    weeks: list[dict]
    rows: list[Row]
    totals: Row
    kpi: dict
    context: dict


# --------------------------------------------------------------------------- настройки


def load_app_settings(session: Session) -> AppSettings:
    settings = session.get(AppSettings, 1)
    if settings is None:
        settings = AppSettings(id=1)
        session.add(settings)
        session.commit()
    return settings


def calc_settings(settings: AppSettings) -> CalcSettings:
    return CalcSettings(
        over_coverage_weeks=settings.over_coverage_weeks,
        short_horizon_weeks=settings.short_horizon_weeks,
        demand_avg_weeks=settings.demand_avg_weeks,
        horizon_weeks=settings.horizon_weeks,
    )


def resolve_last_closed_week(settings: AppSettings, today: date | None = None) -> str:
    """Последняя закрытая неделя: из настроек, иначе предыдущая календарная."""
    if settings.last_closed_week:
        return settings.last_closed_week
    return shift_week(week_key(today or date.today()), -1)


def build_week_axis(
    last_closed: str, history: int, horizon: int, today: date | None = None
) -> list[dict]:
    """Ось недель: немного истории слева, плановый горизонт справа."""
    start = shift_week(last_closed, -max(0, history - 1))
    keys = week_range(start, max(0, history - 1) + 1 + horizon)
    current = week_key(today or date.today())
    axis: list[dict] = []
    for key in keys:
        begins = week_start(key)
        axis.append(
            {
                "key": key,
                "year": begins.year,
                "iso_week": int(key.split("W")[1]),
                "start": begins.isoformat(),
                "month_label": f"{MONTHS_RU[begins.month - 1]} {begins.year}",
                "is_closed": key <= last_closed,
                "is_current": key == current,
            }
        )
    return axis


# ----------------------------------------------------------------------------- загрузка


def load_pairs(session: Session, filters: MatrixFilters) -> list[Pair]:
    stmt = (
        select(StoreProduct)
        .join(Store, Store.id == StoreProduct.store_id)
        .join(Product, Product.id == StoreProduct.product_id)
        .outerjoin(Category, Category.id == Product.category_id)
        .options(
            selectinload(StoreProduct.store),
            selectinload(StoreProduct.product).selectinload(Product.category),
        )
        .where(StoreProduct.is_active.is_(True), Store.is_active.is_(True))
    )
    if filters.store_id:
        stmt = stmt.where(StoreProduct.store_id == filters.store_id)
    if filters.category_id:
        stmt = stmt.where(Product.category_id == filters.category_id)
    if filters.product_id:
        stmt = stmt.where(StoreProduct.product_id == filters.product_id)
    if filters.search:
        pattern = f"%{filters.search.strip().lower()}%"
        stmt = stmt.where(
            func_lower(Product.title).like(pattern)
            | func_lower(Product.vendor_code).like(pattern)
            | func_lower(StoreProduct.nm_id).like(pattern)
        )
    stmt = stmt.order_by(Store.code, Product.vendor_code)

    pairs: list[Pair] = []
    for sp in session.scalars(stmt).all():
        category = sp.product.category
        pairs.append(
            Pair(
                id=sp.id,
                store_id=sp.store_id,
                store_name=sp.store.name,
                product_id=sp.product_id,
                vendor_code=sp.product.vendor_code,
                title=sp.product.title,
                nm_id=sp.nm_id,
                category_id=category.id if category else None,
                category_name=category.name if category else None,
                niche=category.niche if category else None,
                abc_class=category.abc_class if category else None,
                base_potential=sp.base_potential,
            )
        )
    return pairs


def func_lower(column):
    from sqlalchemy import func as sa_func

    return sa_func.lower(sa_func.coalesce(column, ""))


def build_series_inputs(
    session: Session,
    pairs: list[Pair],
    weeks: list[str],
    last_closed: str,
) -> dict[int, SeriesInput]:
    """Собирает вход расчёта для каждой пары одним набором запросов."""
    if not pairs:
        return {}

    pair_ids = [p.id for p in pairs]
    before_first = shift_week(weeks[0], -1)

    facts: dict[int, dict[str, WeekFact]] = defaultdict(dict)
    opening: dict[int, float] = {}
    fact_rows = session.scalars(
        select(WeekFact).where(
            WeekFact.store_product_id.in_(pair_ids),
            WeekFact.week <= weeks[-1],
        )
    ).all()
    for row in fact_rows:
        if row.week in weeks:
            facts[row.store_product_id][row.week] = row
        if row.week == before_first and row.stock_end_units is not None:
            opening[row.store_product_id] = row.stock_end_units

    supplies: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    in_transit: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for row in session.scalars(
        select(Supply).where(Supply.store_product_id.in_(pair_ids), Supply.week.in_(weeks))
    ).all():
        supplies[row.store_product_id][row.week] += row.qty
        # Поставка в незакрытой неделе — всё ещё ожидаемый приход, какой бы статус
        # ей ни проставили руками: факта по этой неделе ещё нет, и в остатке её нет.
        # Иначе заказ, отмеченный принятым до недели прихода, исчезал бы из расчёта
        # и потребность возвращалась бы на пустом месте.
        if row.week > last_closed or row.status in (SupplyStatus.PLANNED, SupplyStatus.IN_TRANSIT):
            in_transit[row.store_product_id][row.week] += row.qty

    plans: dict[int, dict[str, float]] = defaultdict(dict)
    for row in session.scalars(
        select(WeekPlan).where(WeekPlan.store_product_id.in_(pair_ids), WeekPlan.week.in_(weeks))
    ).all():
        plans[row.store_product_id][row.week] = row.plan_units

    adjustments: dict[int, list[ForecastAdjustment]] = defaultdict(list)
    for row in session.scalars(
        select(ForecastAdjustment).where(
            ForecastAdjustment.store_product_id.in_(pair_ids),
            ForecastAdjustment.week.in_(weeks),
        )
    ).all():
        adjustments[row.store_product_id].append(row)

    seasonality: dict[int, dict[int, float]] = defaultdict(dict)
    for row in session.scalars(select(Seasonality)).all():
        seasonality[row.category_id][row.iso_week] = row.coefficient

    out: dict[int, SeriesInput] = {}
    for pair in pairs:
        pair_facts = facts.get(pair.id, {})
        coefs = seasonality.get(pair.category_id or -1, {})
        data = SeriesInput(
            weeks=list(weeks),
            opening_stock=opening.get(pair.id, 0.0),
            last_closed_week=last_closed,
            base_potential=pair.base_potential,
            seasonality={w: coefs.get(int(w.split("W")[1]), 1.0) for w in weeks},
            plan=dict(plans.get(pair.id, {})),
            fact_sales={w: f.sales_units for w, f in pair_facts.items()},
            fact_stock_end={
                w: f.stock_end_units for w, f in pair_facts.items() if f.stock_end_units is not None
            },
            supply=dict(supplies.get(pair.id, {})),
            in_transit=dict(in_transit.get(pair.id, {})),
        )
        for adj in adjustments.get(pair.id, []):
            if adj.diff_percent is not None:
                data.diff_percent[adj.week] = adj.diff_percent
            if adj.manual_forecast is not None:
                data.manual_forecast[adj.week] = adj.manual_forecast
            if adj.manual_adapted is not None:
                data.manual_adapted[adj.week] = adj.manual_adapted
        out[pair.id] = data
    return out


# ------------------------------------------------------------------------------ сборка


def _row_metrics(result: SeriesResult, weeks: list[str]) -> dict[str, dict[str, float | None]]:
    cells = result.by_week()
    metrics: dict[str, dict[str, float | None]] = {m: {} for m in METRICS}
    flags: dict[str, dict[str, bool]] = {"stock_limited": {}, "is_manual": {}}
    for week in weeks:
        cell = cells.get(week)
        if cell is None:
            for m in METRICS:
                metrics[m][week] = None
            continue
        metrics["original_forecast"][week] = cell.original_forecast
        metrics["adapted_forecast"][week] = cell.adapted_forecast
        metrics["real_stock"][week] = cell.real_stock
        metrics["adapted_stock"][week] = cell.adapted_stock
        metrics["fact_sales"][week] = cell.fact_sales
        metrics["supply"][week] = cell.supply
        metrics["diff_percent"][week] = cell.diff_percent
        metrics["shortage_units"][week] = cell.shortage_units
        flags["stock_limited"][week] = cell.stock_limited
        flags["is_manual"][week] = cell.is_manual
    metrics.update(flags)  # type: ignore[arg-type]
    return metrics


def build_matrix(
    session: Session,
    level: Level,
    filters: MatrixFilters,
    today: date | None = None,
) -> Matrix:
    today = today or date.today()
    app_settings = load_app_settings(session)
    settings = calc_settings(app_settings)
    last_closed = resolve_last_closed_week(app_settings, today)
    axis = build_week_axis(
        last_closed, app_settings.history_weeks, app_settings.horizon_weeks, today
    )
    weeks = [w["key"] for w in axis]

    pairs = load_pairs(session, filters)
    inputs = build_series_inputs(session, pairs, weeks, last_closed)
    results: dict[int, SeriesResult] = {
        pair.id: compute_series(inputs[pair.id], settings) for pair in pairs
    }

    if level is Level.SKU:
        rows = [
            Row(
                key=f"pair-{pair.id}",
                title=pair.title,
                subtitle=f"арт. {pair.vendor_code}",
                status=results[pair.id].status,
                meta={
                    "pair_id": pair.id,
                    "store_id": pair.store_id,
                    "store_name": pair.store_name,
                    "vendor_code": pair.vendor_code,
                    "product_id": pair.product_id,
                    "category": pair.category_name,
                    "abc_class": pair.abc_class,
                    "nm_id": pair.nm_id,
                    "coverage_weeks": results[pair.id].coverage_weeks,
                    "editable": True,
                },
                metrics=_row_metrics(results[pair.id], weeks),
            )
            for pair in pairs
        ]
    else:
        groups: dict[tuple, list[Pair]] = defaultdict(list)
        for pair in pairs:
            if level is Level.STORE:
                groups[(pair.store_id, pair.store_name)].append(pair)
            else:
                groups[(pair.category_id, pair.category_name or "Без категории")].append(pair)
        rows = []
        for (key, title), group in sorted(groups.items(), key=lambda kv: str(kv[0][1])):
            agg = aggregate([results[p.id] for p in group], weeks)
            rows.append(
                Row(
                    key=f"{level.value}-{key}",
                    title=title,
                    subtitle=f"{len(group)} SKU",
                    status=agg.status,
                    meta={
                        "group_id": key,
                        "sku_count": len(group),
                        "editable": False,
                    },
                    metrics=_row_metrics(agg, weeks),
                )
            )

    total = aggregate(list(results.values()), weeks)
    totals = Row(
        key="total",
        title=_totals_title(level, filters),
        status=total.status,
        meta={"sku_count": len(pairs), "editable": False},
        metrics=_row_metrics(total, weeks),
    )

    return Matrix(
        level=level,
        weeks=axis,
        rows=rows,
        totals=totals,
        kpi=build_kpi(results, weeks, last_closed, app_settings),
        context={
            "last_closed_week": last_closed,
            "horizon_weeks": app_settings.horizon_weeks,
            "history_weeks": app_settings.history_weeks,
            "sku_count": len(pairs),
            "filters": {
                "store_id": filters.store_id,
                "category_id": filters.category_id,
                "product_id": filters.product_id,
                "search": filters.search,
            },
        },
    )


def _totals_title(level: Level, filters: MatrixFilters) -> str:
    if level is Level.STORE:
        return "Итого по WB"
    if level is Level.CATEGORY:
        return "Итого по магазину" if filters.store_id else "Итого по всем магазинам"
    return "Итого по выборке"


def build_kpi(
    results: dict[int, SeriesResult],
    weeks: list[str],
    last_closed: str,
    app_settings: AppSettings,
) -> dict:
    """KPI-плитки с макета заказчика."""
    closed = [w for w in weeks if w <= last_closed]
    future = [w for w in weeks if w > last_closed]
    window = closed[-4:]

    plan_sum = fact_sum = 0.0
    for result in results.values():
        cells = result.by_week()
        for week in window:
            cell = cells.get(week)
            if cell is None:
                continue
            plan_sum += cell.original_forecast
            fact_sum += cell.fact_sales if cell.fact_sales is not None else 0.0

    next_week = future[0] if future else None
    next_forecast = 0.0
    if next_week:
        next_forecast = sum(
            r.by_week()[next_week].original_forecast
            for r in results.values()
            if next_week in r.by_week()
        )

    short_skus = sum(1 for r in results.values() if r.status is Status.SHORT)
    over_skus = sum(1 for r in results.values() if r.status is Status.OVER)

    return {
        "last_closed_week": last_closed,
        "plan_window_weeks": len(window),
        "plan_units": plan_sum,
        "fact_units": fact_sum,
        "plan_completion_percent": round(fact_sum / plan_sum * 100, 1) if plan_sum else None,
        "plan_delta_units": fact_sum - plan_sum,
        "next_week": next_week,
        "next_week_forecast": next_forecast,
        "short_skus": short_skus,
        "over_skus": over_skus,
        "sku_count": len(results),
    }
