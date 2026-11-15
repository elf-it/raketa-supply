"""Схемы HTTP API."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from .calc import BulkOperation, Level, Status
from .models import Marketplace, OrderStatus, Role


def _with_utc(value: datetime) -> datetime:
    """Помечает время как UTC.

    База хранит отметки времени в UTC, но без зоны. Браузер разбирает такую строку
    как местное время, и момент съезжает на часовой пояс — заказ, созданный в 06:56,
    показывался как 02:56. Зона проставляется один раз здесь, на границе HTTP.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


#: Отметка времени, уезжающая в JSON с явной зоной.
UtcDatetime = Annotated[datetime, AfterValidator(_with_utc)]


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
    #: Когда завели учётную запись. На экране пользователей по этой отметке видно,
    #: кто появился недавно, а кто остался с прошлого состава.
    created_at: UtcDatetime


class PasswordChangeIn(BaseModel_):
    """Смена собственного пароля: прежний подтверждает, что это тот же человек."""

    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


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
    #: С какой даты действуют нынешние цены (задача B-9). Пусто — истории цен
    #: нет: товар достался системе с ценой, но без даты её появления.
    price_valid_from: date | None = None
    # Дальше — для экрана справочников (задача B-14): идентификаторы связей нужны
    # формам правки, а число кабинетов отвечает на вопрос «а он вообще считается».
    category_id: int | None = None
    supplier_id: int | None = None
    coverage_weeks: int | None = None
    safety_weeks: float | None = None
    is_active: bool = True
    store_count: int = 0


class PriceIn(BaseModel_):
    """Новая цена товара с даты.

    Передать можно одно значение из двух — второе останется прежним. Автора
    клиент не передаёт: он берётся из сессии.
    """

    valid_from: date
    cost_price: float | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)


class PriceOut(BaseModel_):
    id: int
    valid_from: date
    cost_price: float
    price: float
    author: str | None = None
    note: str | None = None
    #: Когда запись завели: о подорожании узнают позже, чем оно случилось.
    created_at: UtcDatetime


class PriceCardOut(BaseModel_):
    """Товар с его историей цен: ответ на любое изменение истории."""

    product_id: int
    vendor_code: str
    title: str
    supplier: str | None = None
    #: Действует сейчас — то же, что в карточке товара.
    cost_price: float
    price: float
    valid_from: date | None = None
    history: list[PriceOut] = []


# ------------------------------------------- ведение справочников (задача B-14)
#
# Цен в этих схемах нет намеренно: действующие значения карточки пишет только
# сервис цен, иначе карточка и история разойдутся. Исключение — начальная цена
# при заведении товара: она проводится через тот же сервис и становится первой
# записью истории.


class SupplierOut(BaseModel_):
    id: int
    name: str
    lead_time_days: int
    moq: int
    product_count: int = 0


class StoreIn(BaseModel_):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    marketplace: Marketplace = Marketplace.WB
    #: Внешний идентификатор кабинета; ключ API задаётся только в окружении.
    cabinet_ref: str | None = Field(default=None, max_length=64)


class StoreUpdateIn(BaseModel_):
    """Код кабинета не меняется: по нему ищется ключ API в ``WB_TOKENS``."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    marketplace: Marketplace | None = None
    cabinet_ref: str | None = Field(default=None, max_length=64)
    is_active: bool | None = None


class SupplierIn(BaseModel_):
    name: str = Field(min_length=1, max_length=128)
    lead_time_days: int = Field(default=14, ge=0, le=365)
    moq: int = Field(default=0, ge=0)


class SupplierUpdateIn(BaseModel_):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    moq: int | None = Field(default=None, ge=0)


class CategoryIn(BaseModel_):
    name: str = Field(min_length=1, max_length=128)
    niche: str | None = Field(default=None, max_length=128)
    abc_class: str | None = Field(default=None, max_length=2)


class CategoryUpdateIn(BaseModel_):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    niche: str | None = Field(default=None, max_length=128)
    abc_class: str | None = Field(default=None, max_length=2)


class ProductIn(BaseModel_):
    """Новая номенклатура.

    Пустые ограничения закупки означают «брать из поставщика или настроек» —
    это не то же самое, что ноль.
    """

    vendor_code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    category_id: int | None = None
    supplier_id: int | None = None
    volume_m3: float = Field(default=0.0, ge=0)
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    moq: int | None = Field(default=None, ge=0)
    pack_size: int | None = Field(default=None, ge=1)
    coverage_weeks: int | None = Field(default=None, ge=0, le=104)
    safety_weeks: float | None = Field(default=None, ge=0, le=104)
    #: Начальная себестоимость и цена: уйдут в историю цен первой записью.
    cost_price: float | None = Field(default=None, ge=0)
    price: float | None = Field(default=None, ge=0)


class ProductUpdateIn(BaseModel_):
    """Карточка без цен: себестоимость и цена правятся на экране цен.

    ``clear`` снимает ограничение обратно к значению по умолчанию: отличить
    «не передали поле» от «обнулить» иначе нельзя.

    Лишние поля не принимаются — ровно ради цен: попытка записать себестоимость
    через карточку должна получить отказ, а не молчание. Молча пропущенная цена
    выглядела бы как сохранённая, и человек ушёл бы с экрана, считая дело
    сделанным, пока карточка и история цен расходятся.
    """

    model_config = ConfigDict(from_attributes=True, extra="forbid")

    vendor_code: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    category_id: int | None = None
    supplier_id: int | None = None
    volume_m3: float | None = Field(default=None, ge=0)
    lead_time_days: int | None = Field(default=None, ge=0, le=365)
    moq: int | None = Field(default=None, ge=0)
    pack_size: int | None = Field(default=None, ge=1)
    coverage_weeks: int | None = Field(default=None, ge=0, le=104)
    safety_weeks: float | None = Field(default=None, ge=0, le=104)
    is_active: bool | None = None
    clear: list[str] = []


class PairIn(BaseModel_):
    """Товар в кабинете — минимальная расчётная единица."""

    store_id: int
    #: Идентификатор номенклатуры в кабинете WB: по нему загрузка находит пару.
    nm_id: str | None = Field(default=None, max_length=32)
    barcode: str | None = Field(default=None, max_length=64)
    base_potential: float = Field(default=0.0, ge=0)


class PairUpdateIn(BaseModel_):
    nm_id: str | None = Field(default=None, max_length=32)
    barcode: str | None = Field(default=None, max_length=64)
    base_potential: float | None = Field(default=None, ge=0)
    is_active: bool | None = None


class PairOut(BaseModel_):
    id: int
    store_id: int
    store_code: str
    store_name: str
    store_active: bool
    nm_id: str | None = None
    barcode: str | None = None
    base_potential: float
    is_active: bool
    #: Сколько недельных фактов уже привязано к паре: по этому числу видно,
    #: можно ли её удалить или остаётся только отключить.
    fact_count: int = 0


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
    created_at: UtcDatetime


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
    created_at: UtcDatetime
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
    #: Нужен экрану, чтобы сопоставить позицию с черновиком того же поставщика:
    #: по названию такое сопоставление сломали бы два тёзки в справочнике.
    supplier_id: int | None = None
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


class OrderLineIn(BaseModel_):
    pair_id: int
    qty: int = Field(gt=0)
    arrival_week: str = Field(min_length=8, max_length=8)


class OrderIn(BaseModel_):
    lines: list[OrderLineIn] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class OrderLinePatchIn(BaseModel_):
    """Правка строки черновика: меняется то, что передано, остальное остаётся."""

    qty: int | None = Field(default=None, gt=0)
    arrival_week: str | None = Field(default=None, min_length=8, max_length=8)


class OrderNoteIn(BaseModel_):
    note: str | None = Field(default=None, max_length=1000)


class OrderStatusIn(BaseModel_):
    status: OrderStatus


class OrderLineOut(BaseModel_):
    id: int
    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    qty: int
    arrival_week: str
    cost: float
    volume_m3: float
    #: Себестоимость, записанная в строку при её создании.
    cost_price: float = 0.0
    #: Себестоимость, действующая сейчас (задача B-9). Отличается от записанной —
    #: значит, цена изменилась после того, как строку собрали, и сумма черновика
    #: устарела. Документ от этого не меняется: его сумму правит человек.
    cost_price_now: float = 0.0


class OrderOut(BaseModel_):
    id: int
    status: OrderStatus
    status_label: str
    supplier: str | None = None
    supplier_id: int | None = None
    note: str | None = None
    created_at: UtcDatetime
    created_by: str | None = None
    placed_at: UtcDatetime | None = None
    closed_at: UtcDatetime | None = None
    lines_count: int
    total_qty: int
    total_cost: float
    total_volume_m3: float
    #: Статусы, в которые заказ можно перевести прямо сейчас.
    next_statuses: list[OrderStatus] = []
    lines: list[OrderLineOut] = []


class ImportChangeOut(BaseModel_):
    pair_id: int
    store_name: str
    vendor_code: str
    title: str
    week: str
    old_value: float
    new_value: float


class ImportConflictOut(ImportChangeOut):
    #: Кто и когда поменял эту ячейку уже после того, как файл выгрузили.
    changed_at: UtcDatetime
    changed_by: str | None = None


class ImportIssueOut(BaseModel_):
    reason: str
    count: int
    examples: list[str] = []


class MatrixImportOut(BaseModel_):
    """Что файл меняет и что из него не взяли. Один и тот же ответ на разбор и на применение."""

    exported_at: UtcDatetime
    applied: bool
    rows_read: int
    cells_read: int
    change_count: int
    conflict_count: int
    applied_cells: int
    batch_id: str | None = None
    #: Начало списков: показать тысячу строк всё равно некуда.
    changes: list[ImportChangeOut] = []
    conflicts: list[ImportConflictOut] = []
    issues: list[ImportIssueOut] = []


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
