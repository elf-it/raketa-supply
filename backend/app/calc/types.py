"""Структуры входа и выхода расчётного ядра.

Ядро не знает ни про базу, ни про HTTP: на вход — эти dataclass'ы, на выход — тоже.
Благодаря этому тест-кейсы приёмки и сверка с действующими файлами заказчика гоняются
как обычные юнит-тесты.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Level(str, Enum):
    """Уровень просмотра недельной матрицы (ТЗ, стр. 3)."""

    STORE = "store"
    CATEGORY = "category"
    SKU = "sku"


class Status(str, Enum):
    OK = "OK"
    SHORT = "SHORT"
    OVER = "OVER"


class BulkOperation(str, Enum):
    """Операции массовой корректировки прогноза (задача C-14).

    Словарь предметной области, общий для сервисов и HTTP-схем, поэтому живёт здесь,
    рядом с :class:`Level` — иначе схемам пришлось бы импортировать сервисы.
    """

    #: Умножить прогноз на процент.
    SCALE = "scale"
    #: Прибавить штуки.
    DELTA = "delta"
    #: Поставить значение.
    SET = "set"
    #: Сбросить ручные правки и вернуться к формуле.
    CLEAR = "clear"
    #: Перенести спрос выбранных недель в целевую.
    MOVE = "move"


@dataclass(frozen=True)
class CalcSettings:
    """Глобальные настройки расчёта.

    ``over_coverage_weeks`` — порог затоваривания: во сколько недель спроса
    укладывается текущий остаток.

    ``short_horizon_weeks`` — окно, в котором дефицит считается статусом SHORT.
    Без него на длинном горизонте без плановых закупок дефицит наступает у любой
    позиции и статус перестаёт что-либо различать.

    Значения в ТЗ не заданы, см. вопрос Q-1.
    """

    over_coverage_weeks: float = 12.0
    short_horizon_weeks: int = 8
    demand_avg_weeks: int = 4
    horizon_weeks: int = 52


@dataclass(frozen=True)
class PurchaseParams:
    """Ограничения закупки по позиции (схема 03 из ТЗ)."""

    lead_time_days: int = 14
    moq: int = 0
    pack_size: int = 1
    coverage_weeks: int = 4
    safety_weeks: float = 2.0
    cost_price: float = 0.0
    #: Цена продажи — нужна только для ROI (задача C-11). В расчёт количества
    #: она не входит: сколько покупать, решает спрос, а не маржа.
    price: float = 0.0
    volume_m3: float = 0.0


@dataclass
class SeriesInput:
    """Вход расчёта для одной пары (магазин, товар).

    ``weeks`` — горизонт по возрастанию, включая закрытые недели, которые нужно
    показать в матрице. ``opening_stock`` — физический остаток на конец недели,
    предшествующей ``weeks[0]``.
    """

    weeks: list[str]
    opening_stock: float = 0.0
    last_closed_week: str | None = None
    base_potential: float = 0.0
    seasonality: dict[str, float] = field(default_factory=dict)
    diff_percent: dict[str, float] = field(default_factory=dict)
    manual_forecast: dict[str, float] = field(default_factory=dict)
    manual_adapted: dict[str, float] = field(default_factory=dict)
    plan: dict[str, float] = field(default_factory=dict)
    fact_sales: dict[str, float] = field(default_factory=dict)
    fact_stock_end: dict[str, float] = field(default_factory=dict)
    supply: dict[str, float] = field(default_factory=dict)
    in_transit: dict[str, float] = field(default_factory=dict)

    def is_closed(self, week: str) -> bool:
        return self.last_closed_week is not None and week <= self.last_closed_week


@dataclass(slots=True)
class WeekCell:
    """Одна неделя одной строки матрицы.

    ``slots=True`` здесь не украшение: ячеек столько же, сколько пар на недели,
    и на кэшируемом каталоге их сотни тысяч. Без слотов каждая тащила бы ещё
    и словарь атрибутов — по замерам это разница в три с половиной раза
    по памяти кэша.
    """

    week: str
    is_closed: bool = False
    original_forecast: float = 0.0
    adapted_forecast: float = 0.0
    real_stock: float = 0.0
    adapted_stock: float = 0.0
    fact_sales: float | None = None
    supply: float = 0.0
    #: Ожидаемый приход этой недели: часть ``supply``, которой ещё не было
    #: в остатке. Нужен расчёту закупки, и держится здесь, чтобы готовый ряд
    #: не требовал заново поднимать из базы то, из чего он уже посчитан.
    in_transit: float = 0.0
    diff_percent: float | None = None
    shortage_units: float = 0.0
    stock_limited: bool = False
    is_manual: bool = False


@dataclass
class SeriesResult:
    cells: list[WeekCell]
    status: Status = Status.OK
    coverage_weeks: float | None = None
    #: Остаток на входе в первую неделю ряда — он же остаток, с которым считается
    #: закупка, если в ряду нет ни одной закрытой недели.
    opening_stock: float = 0.0
    _by_week: dict[str, WeekCell] | None = field(default=None, repr=False, compare=False)

    def by_week(self) -> dict[str, WeekCell]:
        """Ячейки по неделям. Словарь собирается один раз: его просят многократно —
        агрегация, сборка строк, KPI, — и каждый раз пересобирать его означало бы
        тысячи словарей на запрос."""
        if self._by_week is None:
            self._by_week = {c.week: c for c in self.cells}
        return self._by_week


@dataclass
class PurchaseSuggestion:
    """Результат расчёта закупки: штуки, стоимость, м³, приход (схема 03 из ТЗ)."""

    needed: bool
    shortage_week: str | None = None
    order_date: date | None = None
    arrival_week: str | None = None
    qty: int = 0
    qty_before_constraints: float = 0.0
    cost: float = 0.0
    volume_m3: float = 0.0
    safety_stock: float = 0.0
    is_overdue: bool = False
    reason: str = ""
    #: Разбор количества: окно покрытия, спрос внутри него, партии в пути внутри него
    #: и остаток на входе в окно. Экран показывает цифру теми же числами, которыми она
    #: посчитана; без этого объяснение пришлось бы собирать второй реализацией формулы.
    coverage_from: str | None = None
    coverage_to: str | None = None
    coverage_weeks_used: int = 0
    window_demand: float = 0.0
    window_transit: float = 0.0
    stock_before: float = 0.0
    #: Доходность вложенных в этот заказ денег, процентов годовых, и из чего она
    #: сложилась: за сколько недель заказ распродаётся и сколько недель деньги
    #: заняты целиком (задача C-11). ``None`` — считать не из чего: нет цены,
    #: себестоимости или спроса. Это показатель, а не правило: на количество,
    #: дату заказа и на то, попадёт ли позиция в план, он не влияет.
    roi_percent: float | None = None
    sell_out_weeks: float | None = None
    cycle_weeks: float | None = None
