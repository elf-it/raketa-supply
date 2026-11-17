"""Сборка недельной матрицы: БД → расчётное ядро → строки для интерфейса."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

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
from .calc_cache import get_series_cache, series_signature

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
    #: Номер версии данных пары: по нему кэш расчёта решает, годится ли
    #: посчитанный ранее ряд. Читается вместе с парой — раньше самих данных,
    #: и на этом порядке держится правильность кэша (`services/calc_cache.py`).
    calc_revision: int = 1


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
    #: Сколько строк в выборке целиком: ``rows`` может быть окном из них.
    row_count: int = 0
    #: С какой строки выборки начинается ``rows``.
    offset: int = 0


def _window(items: list, offset: int, limit: int | None) -> list:
    """Окно строк: ``limit=None`` — вся выборка, начиная с ``offset``."""
    if limit is None:
        return items[offset:]
    return items[offset : offset + limit]


def _pick_rows(
    items: list,
    offset: int,
    limit: int | None,
    holds_pair: Callable[[object], bool] | None,
) -> tuple[list, int]:
    """Какие строки попадут в ответ — и с какого места выборки они начинаются.

    Обычно это окно по ``offset`` и ``limit``. Если задан ``holds_pair``, просят
    одну строку — ту, в которой живёт пара; вместе с ней возвращается её место
    в выборке, потому что экрану нужно знать не только что в строке, но и на какую
    строку её класть. Пары в выборке нет — список пустой: при отборе по статусу
    правка может вытолкнуть строку из выборки, и это не ошибка.
    """
    if holds_pair is None:
        return _window(items, offset, limit), offset
    for index, item in enumerate(items):
        if holds_pair(item):
            return [item], index
    return [], 0


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
    """Пары выборки одним запросом — колонками, а не сущностями.

    Объекты здесь не нужны: :class:`Pair` — ровно то, что требуется расчёту
    и шапке строки, а сборка тысячи ``StoreProduct`` с подгруженными магазином,
    товаром и категорией стоила бы на порядок дороже самого запроса.

    Колонки заодно дают свежий ``calc_revision``: сущность, однажды загруженную
    в сессию, повторный запрос не перечитывает, и в сессии, которая только что
    сама же что-то правила, номер остался бы прежним.
    """
    stmt = (
        select(
            StoreProduct.id.label("id"),
            StoreProduct.store_id.label("store_id"),
            Store.name.label("store_name"),
            StoreProduct.product_id.label("product_id"),
            Product.vendor_code.label("vendor_code"),
            Product.title.label("title"),
            StoreProduct.nm_id.label("nm_id"),
            Category.id.label("category_id"),
            Category.name.label("category_name"),
            Category.niche.label("niche"),
            Category.abc_class.label("abc_class"),
            StoreProduct.base_potential.label("base_potential"),
            StoreProduct.calc_revision.label("calc_revision"),
        )
        .join(Store, Store.id == StoreProduct.store_id)
        .join(Product, Product.id == StoreProduct.product_id)
        .outerjoin(Category, Category.id == Product.category_id)
        # Снятый с продажи товар и отключённый кабинет из расчёта выпадают вместе
        # со своими парами (задача B-14): иначе «снять с продажи» в справочнике
        # ничего бы не значило, и позиция продолжала бы просить закупку.
        .where(
            StoreProduct.is_active.is_(True),
            Store.is_active.is_(True),
            Product.is_active.is_(True),
        )
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

    # Имена колонок совпадают с полями ``Pair`` намеренно: подстановка по имени
    # не переставит ниши с ABC-классом, если в запрос добавят ещё колонку.
    return [Pair(**row._mapping) for row in session.execute(stmt)]


def func_lower(column):
    from sqlalchemy import func as sa_func

    return sa_func.lower(sa_func.coalesce(column, ""))


def build_series_inputs(
    session: Session,
    pairs: list[Pair],
    weeks: list[str],
    last_closed: str | None,
) -> dict[int, SeriesInput]:
    """Собирает вход расчёта для каждой пары одним набором запросов.

    Запросы берут колонки, а не сущности: из недельного факта расчёту нужны
    два числа, а ORM собрала бы на каждую строку объект с отслеживанием
    изменений. На тысяче пар таких строк больше тридцати тысяч, и их сборка
    была самой дорогой частью ответа.
    """
    if not pairs:
        return {}

    pair_ids = [p.id for p in pairs]
    week_set = set(weeks)
    before_first = shift_week(weeks[0], -1)
    # Пока ни одна неделя не закрыта, прийти ещё ничего не успело: пустая
    # отметка должна быть меньше любой недели, а не ломать сравнение.
    closed_until = last_closed or ""

    fact_sales: dict[int, dict[str, float]] = defaultdict(dict)
    fact_stock: dict[int, dict[str, float]] = defaultdict(dict)
    opening: dict[int, float] = {}
    for pair_id, week, sales, stock_end in session.execute(
        select(
            WeekFact.store_product_id,
            WeekFact.week,
            WeekFact.sales_units,
            WeekFact.stock_end_units,
        ).where(
            WeekFact.store_product_id.in_(pair_ids),
            WeekFact.week.in_([*weeks, before_first]),
        )
    ):
        if week in week_set:
            fact_sales[pair_id][week] = sales
            if stock_end is not None:
                fact_stock[pair_id][week] = stock_end
        elif week == before_first and stock_end is not None:
            opening[pair_id] = stock_end

    supplies: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    in_transit: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for pair_id, week, qty, status in session.execute(
        select(Supply.store_product_id, Supply.week, Supply.qty, Supply.status).where(
            Supply.store_product_id.in_(pair_ids), Supply.week.in_(weeks)
        )
    ):
        supplies[pair_id][week] += qty
        # Поставка в незакрытой неделе — всё ещё ожидаемый приход, какой бы статус
        # ей ни проставили руками: факта по этой неделе ещё нет, и в остатке её нет.
        # Иначе заказ, отмеченный принятым до недели прихода, исчезал бы из расчёта
        # и потребность возвращалась бы на пустом месте.
        if week > closed_until or status in (SupplyStatus.PLANNED, SupplyStatus.IN_TRANSIT):
            in_transit[pair_id][week] += qty

    plans: dict[int, dict[str, float]] = defaultdict(dict)
    for pair_id, week, plan_units in session.execute(
        select(WeekPlan.store_product_id, WeekPlan.week, WeekPlan.plan_units).where(
            WeekPlan.store_product_id.in_(pair_ids), WeekPlan.week.in_(weeks)
        )
    ):
        plans[pair_id][week] = plan_units

    diffs: dict[int, dict[str, float]] = defaultdict(dict)
    manual_forecast: dict[int, dict[str, float]] = defaultdict(dict)
    manual_adapted: dict[int, dict[str, float]] = defaultdict(dict)
    for pair_id, week, diff, forecast, adapted in session.execute(
        select(
            ForecastAdjustment.store_product_id,
            ForecastAdjustment.week,
            ForecastAdjustment.diff_percent,
            ForecastAdjustment.manual_forecast,
            ForecastAdjustment.manual_adapted,
        ).where(
            ForecastAdjustment.store_product_id.in_(pair_ids),
            ForecastAdjustment.week.in_(weeks),
        )
    ):
        if diff is not None:
            diffs[pair_id][week] = diff
        if forecast is not None:
            manual_forecast[pair_id][week] = forecast
        if adapted is not None:
            manual_adapted[pair_id][week] = adapted

    seasonality: dict[int, dict[int, float]] = defaultdict(dict)
    for category_id, iso_week, coefficient in session.execute(
        select(Seasonality.category_id, Seasonality.iso_week, Seasonality.coefficient)
    ):
        seasonality[category_id][iso_week] = coefficient

    # Номера недель оси считаются один раз на всю выборку, а не на каждую пару.
    iso_weeks = [(w, int(w.split("W")[1])) for w in weeks]

    out: dict[int, SeriesInput] = {}
    for pair in pairs:
        coefs = seasonality.get(pair.category_id or -1, {})
        out[pair.id] = SeriesInput(
            weeks=list(weeks),
            opening_stock=opening.get(pair.id, 0.0),
            last_closed_week=last_closed,
            base_potential=pair.base_potential,
            seasonality={week: coefs.get(iso, 1.0) for week, iso in iso_weeks},
            diff_percent=dict(diffs.get(pair.id, {})),
            manual_forecast=dict(manual_forecast.get(pair.id, {})),
            manual_adapted=dict(manual_adapted.get(pair.id, {})),
            plan=dict(plans.get(pair.id, {})),
            fact_sales=dict(fact_sales.get(pair.id, {})),
            fact_stock_end=dict(fact_stock.get(pair.id, {})),
            supply=dict(supplies.get(pair.id, {})),
            in_transit=dict(in_transit.get(pair.id, {})),
        )
    return out


def compute_results(
    session: Session,
    pairs: list[Pair],
    weeks: list[str],
    last_closed: str | None,
    settings: CalcSettings,
    epoch: int,
) -> dict[int, SeriesResult]:
    """Недельные ряды по парам: что есть в кэше — оттуда, остальное — расчётом.

    Единственное место, где считаются ряды для экранов, поэтому и кэш один
    на всех: матрица, закупка и выгрузки получают одинаковые числа, а не
    каждая свои.

    Порядок важен: номера версий пришли вместе с ``pairs``, то есть раньше,
    чем грузятся данные. Если правка закоммитится между этими двумя чтениями,
    результат окажется помечен устаревшим номером и его никто не спросит.
    Обратный порядок дал бы свежий номер на старых данных — молча неверные числа;
    почему выбрано так, подробно в `services/calc_cache.py`.
    """
    cache = get_series_cache()
    signature = series_signature(weeks, last_closed, settings)

    # Пачкой, а не по одной: общий кэш (G-14) ходит по сети, и тысяча отдельных
    # походов стоила бы дороже, чем посчитать эти ряды заново.
    results = cache.get_many([(pair.id, signature, pair.calc_revision, epoch) for pair in pairs])
    misses = [pair for pair in pairs if pair.id not in results]

    if misses:
        inputs = build_series_inputs(session, misses, weeks, last_closed)
        computed = []
        for pair in misses:
            result = compute_series(inputs[pair.id], settings)
            computed.append((pair.id, signature, pair.calc_revision, epoch, result))
            results[pair.id] = result
        cache.put_many(computed)
    return results


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
    status: Status | None = None,
    offset: int = 0,
    limit: int | None = None,
    pair_id: int | None = None,
) -> Matrix:
    """Матрица по уровню просмотра.

    ``status`` отбирает строки по статусу OVER/SHORT/OK. Он не лежит в
    :class:`MatrixFilters` намеренно: те фильтры сужают выборку пар в запросе к базе,
    а статус — результат расчёта, и отобрать по нему можно только после того, как
    расчёт прошёл. Хранить их рядом значило бы обещать, что ``load_pairs`` умеет
    фильтровать по статусу.

    ``offset`` и ``limit`` задают окно строк. Урезается только ``rows``: итоги, KPI
    и счётчики считаются по всей выборке, иначе итог под таблицей расходился бы
    с тем, что над ним, стоило прокрутить страницу. ``limit=None`` отдаёт выборку
    целиком — так работают выгрузка в XLSX и тесты; окно задаёт клиент, а не сервер:
    сервер обязан уметь отдать всё, экран — не обязан всё просить.

    ``pair_id`` заменяет окно строк одной строкой — той, в которой живёт эта пара
    (на уровне групп это строка её магазина или категории). Так отвечают на правку
    ячейки: пересчиталась одна позиция, и перечитывать ради неё всё загруженное
    незачем. Итоги, KPI и счётчики при этом считаются как обычно, по всей выборке, —
    правка меняет и их.

    Ряды собираются по всем парам выборки, а не только по окну: без них не
    посчитать ни итоги, ни отбор по статусу. Зато посчитаны они будут не заново —
    :func:`compute_results` берёт готовые из кэша и считает только те пары, чьи
    данные изменились. Окно экономит сборку показателей по неделям и передачу.
    """
    today = today or date.today()
    app_settings = load_app_settings(session)
    settings = calc_settings(app_settings)
    last_closed = resolve_last_closed_week(app_settings, today)
    axis = build_week_axis(
        last_closed, app_settings.history_weeks, app_settings.horizon_weeks, today
    )
    weeks = [w["key"] for w in axis]

    pairs = load_pairs(session, filters)
    results = compute_results(session, pairs, weeks, last_closed, settings, app_settings.calc_epoch)

    if level is Level.SKU:
        shown = [pair for pair in pairs if status is None or results[pair.id].status is status]
        row_count = len(shown)
        window, offset = _pick_rows(
            shown, offset, limit, None if pair_id is None else lambda pair: pair.id == pair_id
        )
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
            for pair in window
        ]
    else:
        groups: dict[tuple, list[Pair]] = defaultdict(list)
        for pair in pairs:
            if level is Level.STORE:
                groups[(pair.store_id, pair.store_name)].append(pair)
            else:
                groups[(pair.category_id, pair.category_name or "Без категории")].append(pair)
        visible: list[tuple[object, str, list[Pair], SeriesResult]] = []
        for (key, title), group in sorted(groups.items(), key=lambda kv: str(kv[0][1])):
            agg = aggregate([results[p.id] for p in group], weeks)
            # Статус группы — худший из статусов её позиций, и отбирается группа целиком:
            # магазин с дефицитом остаётся магазином с дефицитом, а не превращается
            # в список своих дефицитных артикулов.
            if status is not None and agg.status is not status:
                continue
            visible.append((key, title, group, agg))
        row_count = len(visible)
        shown = [pair for _, _, group, _ in visible for pair in group]
        window, offset = _pick_rows(
            visible,
            offset,
            limit,
            None if pair_id is None else lambda item: any(p.id == pair_id for p in item[2]),
        )
        rows = [
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
            for key, title, group, agg in window
        ]

    # Итоги и KPI считаются по показанным строкам: отбор по статусу — такая же выборка,
    # как магазин или категория, и итог под таблицей должен сходиться с тем, что над ним.
    shown_results = {pair.id: results[pair.id] for pair in shown}
    total = aggregate(list(shown_results.values()), weeks)
    totals = Row(
        key="total",
        title=_totals_title(level, filters),
        status=total.status,
        meta={"sku_count": len(shown), "editable": False},
        metrics=_row_metrics(total, weeks),
    )

    return Matrix(
        level=level,
        weeks=axis,
        rows=rows,
        totals=totals,
        kpi=build_kpi(shown_results, weeks, last_closed, app_settings),
        row_count=row_count,
        offset=offset,
        context={
            "last_closed_week": last_closed,
            "horizon_weeks": app_settings.horizon_weeks,
            "history_weeks": app_settings.history_weeks,
            "sku_count": len(shown),
            #: Сколько позиций попало под остальные фильтры: по разнице видно,
            #: что именно скрыл отбор по статусу.
            "sku_count_total": len(pairs),
            #: Окно строк: сколько их в выборке и какой кусок отдан.
            "row_count": row_count,
            "row_offset": offset,
            "row_limit": len(rows) if pair_id is not None else limit,
            #: Пары всей выборки, а не только окна. По этому списку работают
            #: «отметить все» и массовая правка: иначе правка «ко всем строкам»
            #: молча накрыла бы одну подгруженную страницу. Это идентификаторы,
            #: не показатели, — на тысяче позиций десяток килобайт.
            "pair_ids": [pair.id for pair in shown] if level is Level.SKU else None,
            "filters": {
                "store_id": filters.store_id,
                "category_id": filters.category_id,
                "product_id": filters.product_id,
                "search": filters.search,
                "status": status.value if status else None,
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
