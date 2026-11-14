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


@dataclass
class WeekCell:
    """Одна неделя одной строки матрицы."""

    week: str
    is_closed: bool = False
    original_forecast: float = 0.0
    adapted_forecast: float = 0.0
    real_stock: float = 0.0
    adapted_stock: float = 0.0
    fact_sales: float | None = None
    supply: float = 0.0
    diff_percent: float | None = None
    shortage_units: float = 0.0
    stock_limited: bool = False
    is_manual: bool = False


@dataclass
class SeriesResult:
    cells: list[WeekCell]
    status: Status = Status.OK
    coverage_weeks: float | None = None

    def by_week(self) -> dict[str, WeekCell]:
        return {c.week: c for c in self.cells}


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
