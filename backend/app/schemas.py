"""Схемы HTTP API."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from .calc import BulkOperation, Level, Status
from .models import Role


class BaseModel_(BaseModel):
    """Базовая схема: разрешает собирать ответ прямо из dataclass'ов сервисов."""

    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel_):
    email: str = Field(min_length=3, max_length=190)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel_):
    id: int
    email: str
    full_name: str
    role: Role
    is_active: bool


class UserCreateIn(BaseModel_):
    email: str = Field(min_length=3, max_length=190)
    full_name: str = Field(min_length=1, max_length=128)
    role: Role = Role.VIEWER
    password: str = Field(min_length=8, max_length=256)


class UserUpdateIn(BaseModel_):
    full_name: str | None = Field(default=None, min_length=1, max_length=128)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class StoreOut(BaseModel_):
    id: int
    code: str
    name: str
    marketplace: str
    is_active: bool


class CategoryOut(BaseModel_):
    id: int
    name: str
    niche: str | None = None
    abc_class: str | None = None


class ProductOut(BaseModel_):
    id: int
    vendor_code: str
    title: str
    category: str | None = None
    supplier: str | None = None
    cost_price: float
    price: float
    volume_m3: float
    lead_time_days: int | None = None
    moq: int | None = None
    pack_size: int | None = None


class WeekOut(BaseModel_):
    key: str
    year: int
    iso_week: int
    start: str
    month_label: str
    is_closed: bool
    is_current: bool


class RowOut(BaseModel_):
    key: str
    title: str
    subtitle: str | None = None
    status: Status
    meta: dict = Field(default_factory=dict)
    metrics: dict[str, dict[str, float | bool | None]] = Field(default_factory=dict)


class KpiOut(BaseModel_):
    last_closed_week: str
    plan_window_weeks: int
    plan_units: float
    fact_units: float
    plan_completion_percent: float | None = None
    plan_delta_units: float
    next_week: str | None = None
    next_week_forecast: float
    short_skus: int
    over_skus: int
    sku_count: int


class MatrixOut(BaseModel_):
    level: Level
    weeks: list[WeekOut]
    rows: list[RowOut]
    totals: RowOut
    kpi: KpiOut
    context: dict


class AdjustmentIn(BaseModel_):
    """Правка ячейки.

    Автора клиент не передаёт: он берётся из сессии. Журнал, в котором автора
    подставляет сам клиент, ничего не доказывает.
    """

    diff_percent: float | None = Field(default=None, ge=0, le=1000)
    manual_forecast: float | None = Field(default=None, ge=0)
    manual_adapted: float | None = Field(default=None, ge=0)
    clear: list[str] = Field(default_factory=list)


class AdjustmentOut(BaseModel_):
    pair_id: int
    week: str
    diff_percent: float | None = None
    manual_forecast: float | None = None
    manual_adapted: float | None = None
    author: str | None = None


class BulkAdjustmentIn(BaseModel_):
    """Массовая правка прогноза (задача C-14).

    `scale` — множитель в процентах, `delta` — штуки со знаком, `set` — значение,
    `clear` — сброс к формуле, `move` — перенос спроса в `target_week`.
    """

    pair_ids: list[int] = Field(min_length=1)
    weeks: list[str] = Field(min_length=1)
    operation: BulkOperation
    value: float | None = None
    target_week: str | None = None


class BulkAdjustmentOut(BaseModel_):
    operation: BulkOperation
    updated_cells: int
    affected_pairs: int
    skipped_closed: int
    moved_units: float
    weeks: list[str]
    target_week: str | None = None
    #: По нему в журнале разворачиваются ячейки этой правки.
    batch_id: str = ""


class JournalEntryOut(BaseModel_):
    """Одно изменение одной ячейки."""

    id: int
    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    category: str | None = None
    week: str
    field: str
    field_label: str
    operation: str
    batch_id: str | None = None
    old_value: float | None = None
    new_value: float | None = None
    author: str | None = None
    created_at: datetime


class JournalPageOut(BaseModel_):
    total: int
    limit: int
    offset: int
    entries: list[JournalEntryOut]


class JournalBatchOut(BaseModel_):
    """Массовая правка, свёрнутая в одну строку."""

    batch_id: str
    operation: str
    author: str | None = None
    created_at: datetime
    cells: int
    pairs: int
    week_from: str
    week_to: str


class JournalBatchPageOut(BaseModel_):
    total: int
    limit: int
    offset: int
    batches: list[JournalBatchOut]


class PurchaseParamsOut(BaseModel_):
    lead_time_days: int
    moq: int
    pack_size: int
    coverage_weeks: int
    safety_weeks: float
    cost_price: float
    volume_m3: float


class PurchaseRowOut(BaseModel_):
    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    category: str | None = None
    supplier: str | None = None
    current_stock: float
    needed: bool
    shortage_week: str | None = None
    order_date: date | None = None
    arrival_week: str | None = None
    qty: int
    qty_before_constraints: float
    cost: float
    volume_m3: float
    safety_stock: float
    is_overdue: bool
    reason: str
    #: Разбор количества, см. PurchaseSuggestion.
    coverage_from: str | None = None
    coverage_to: str | None = None
    coverage_weeks_used: int = 0
    window_demand: float = 0.0
    window_transit: float = 0.0
    stock_before: float = 0.0
    params: PurchaseParamsOut


class PurchasePlanOut(BaseModel_):
    rows: list[PurchaseRowOut]
    total_qty: int
    total_cost: float
    total_volume_m3: float
    overdue_count: int


class BasePotentialIn(BaseModel_):
    base_potential: float = Field(ge=0)


class BasePotentialOut(BaseModel_):
    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    category: str | None = None
    base_potential: float
    #: Ориентир по факту закрытых недель — чтобы потенциал задавался не вслепую.
    fact_weeks: int = 0
    avg_fact_units: float | None = None
    #: Оценка потенциала обратным ходом: факт, делённый на сезонность.
    suggested_potential: float | None = None
    #: Недели, выброшенные из оценки из-за нулевого остатка.
    excluded_weeks: int = 0


class SeasonalityIn(BaseModel_):
    coefficients: dict[int, float]


class SeasonalityOut(BaseModel_):
    category_id: int
    category: str
    coefficients: dict[int, float]


class CloseWeekIn(BaseModel_):
    week: str


class SyncOut(BaseModel_):
    store_code: str
    facts_upserted: int
    unknown_nm_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class SettingsOut(BaseModel_):
    last_closed_week: str | None = None
    horizon_weeks: int
    history_weeks: int
    over_coverage_weeks: float
    short_horizon_weeks: int
    demand_avg_weeks: int
    default_lead_time_days: int
    default_moq: int
    default_pack_size: int
    default_coverage_weeks: int
    default_safety_weeks: float
    wb_client: str
