"""Модель данных единой базы (схема 01 из ТЗ).

Минимальная расчётная единица — пара (магазин, товар), сущность :class:`StoreProduct`.
Все недельные данные привязаны к ней, поэтому один физический остаток принадлежит
ровно одной паре и не может быть учтён дважды при агрегации.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Marketplace(str, enum.Enum):
    WB = "wb"
    OZON = "ozon"
    YM = "ym"


class SupplyStatus(str, enum.Enum):
    PLANNED = "planned"
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"


class OrderStatus(str, enum.Enum):
    """Жизненный цикл заказа поставщику.

    **[ДОПУЩЕНИЕ, Q-11]** Набор статусов выбран минимальным: черновик собирают
    на экране закупки, размещение — момент, после которого товар считается
    заказанным, приёмка закрывает заказ. Подтверждение поставщиком, частичная
    отгрузка и возвраты не разделяются — для них нужен ответ заказчика о том,
    ведётся ли заказ здесь или в учётной системе.
    """

    DRAFT = "draft"
    PLACED = "placed"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class Role(str, enum.Enum):
    """Права доступа (требование приёмки по интерфейсу).

    Роли упорядочены по возрастанию прав: каждая следующая включает предыдущую.
    """

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"


#: Вес роли для сравнения «не ниже, чем».
ROLE_ORDER = {Role.VIEWER: 0, Role.EDITOR: 1, Role.ADMIN: 2}


class SyncStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


# --------------------------------------------------------------------------- справочники


class Store(Base):
    """Кабинет маркетплейса. Данные разных кабинетов никогда не смешиваются."""

    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    marketplace: Mapped[Marketplace] = mapped_column(
        Enum(Marketplace, native_enum=False), default=Marketplace.WB
    )
    #: Внешний идентификатор кабинета; сам ключ API в базе не хранится.
    cabinet_ref: Mapped[str | None] = mapped_column(String(64), default=None)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    pairs: Mapped[list[StoreProduct]] = relationship(back_populates="store")


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    #: Ниша — верхний уровень группировки, фильтр «Все ниши» на макете.
    niche: Mapped[str | None] = mapped_column(String(128), default=None)
    #: Бейдж ABC-класса на макете заказчика.
    abc_class: Mapped[str | None] = mapped_column(String(2), default=None)

    products: Mapped[list[Product]] = relationship(back_populates="category")
    seasonality: Mapped[list[Seasonality]] = relationship(back_populates="category")


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=14)
    moq: Mapped[int] = mapped_column(Integer, default=0)

    products: Mapped[list[Product]] = relationship(back_populates="supplier")


class Product(Base):
    """Товар как единица номенклатуры, общая для всех кабинетов."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Артикул продавца — то, чем товар называют в заказах у поставщика.
    vendor_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"))
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"))

    cost_price: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    volume_m3: Mapped[float] = mapped_column(Float, default=0.0)

    # Ограничения закупки; пустое значение означает «взять из настроек/поставщика».
    lead_time_days: Mapped[int | None] = mapped_column(Integer, default=None)
    moq: Mapped[int | None] = mapped_column(Integer, default=None)
    pack_size: Mapped[int | None] = mapped_column(Integer, default=None)
    coverage_weeks: Mapped[int | None] = mapped_column(Integer, default=None)
    safety_weeks: Mapped[float | None] = mapped_column(Float, default=None)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    category: Mapped[Category | None] = relationship(back_populates="products")
    supplier: Mapped[Supplier | None] = relationship(back_populates="products")
    pairs: Mapped[list[StoreProduct]] = relationship(back_populates="product")


class StoreProduct(Base):
    """Товар в конкретном кабинете — минимальная расчётная единица.

    Здесь же живёт базовый потенциал SKU (ТЗ, п. 03): он задаётся на пару, потому что
    один и тот же товар в разных магазинах продаётся по-разному.
    """

    __tablename__ = "store_products"
    __table_args__ = (UniqueConstraint("store_id", "product_id", name="uq_store_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    #: Идентификатор номенклатуры в кабинете WB (nmId).
    nm_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    barcode: Mapped[str | None] = mapped_column(String(64), default=None)
    base_potential: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Номер версии данных, из которых считается недельный ряд этой пары: факты,
    #: поставки, планы, ручные правки, базовый потенциал. Растёт на любом таком
    #: изменении (`app/revisions.py`), и кэш расчёта хранит результат вместе
    #: с номером. Номер живёт в базе, а не в памяти процесса, поэтому правка,
    #: прошедшая через один воркер, обесценивает кэш у всех остальных.
    calc_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    store: Mapped[Store] = relationship(back_populates="pairs")
    product: Mapped[Product] = relationship(back_populates="pairs")


class Seasonality(Base):
    """Недельный коэффициент сезонности категории (ТЗ, п. 03)."""

    __tablename__ = "seasonality"
    __table_args__ = (UniqueConstraint("category_id", "iso_week", name="uq_seasonality_week"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    #: Номер ISO-недели, 1..53.
    iso_week: Mapped[int] = mapped_column(Integer)
    coefficient: Mapped[float] = mapped_column(Float, default=1.0)

    category: Mapped[Category] = relationship(back_populates="seasonality")


# ----------------------------------------------------------------------------- факты


class WeekFact(Base):
    """Недельный факт по паре (магазин, товар): заказы, выкупы, продажи, остаток."""

    __tablename__ = "week_facts"
    __table_args__ = (UniqueConstraint("store_product_id", "week", name="uq_week_fact"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_product_id: Mapped[int] = mapped_column(ForeignKey("store_products.id"), index=True)
    week: Mapped[str] = mapped_column(String(8), index=True)

    order_units: Mapped[float] = mapped_column(Float, default=0.0)
    buyout_units: Mapped[float] = mapped_column(Float, default=0.0)
    sales_units: Mapped[float] = mapped_column(Float, default=0.0)
    #: Физический остаток на конец недели — источник истины для закрытых недель.
    stock_end_units: Mapped[float | None] = mapped_column(Float, default=None)

    source: Mapped[str] = mapped_column(String(32), default="wb")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class Supply(Base):
    """Поставка: плановая, в пути или принятая."""

    __tablename__ = "supplies"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_product_id: Mapped[int] = mapped_column(ForeignKey("store_products.id"), index=True)
    week: Mapped[str] = mapped_column(String(8), index=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[SupplyStatus] = mapped_column(
        Enum(SupplyStatus, native_enum=False), default=SupplyStatus.PLANNED
    )
    note: Mapped[str | None] = mapped_column(Text, default=None)
    #: Строка заказа, которая породила эту поставку. Пусто — поставка заведена
    #: руками или пришла из маркетплейса.
    order_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("purchase_order_lines.id", ondelete="CASCADE"), index=True, default=None
    )


class PurchaseOrder(Base):
    """Заказ поставщику: документ со статусом.

    Без него закупочная потребность пересчитывается заново при каждом запросе,
    и отметить «заказано» негде. Заказ — единственное место, где фиксируется
    решение человека; расчёт видит его через поставки (:class:`Supply`).
    """

    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), index=True)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, native_enum=False), default=OrderStatus.DRAFT, index=True
    )
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), default=None)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    supplier: Mapped[Supplier | None] = relationship(lazy="joined")
    created_by: Mapped[User | None] = relationship(lazy="joined")
    lines: Mapped[list[PurchaseOrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )


class PurchaseOrderLine(Base):
    """Строка заказа: сколько и чего заказали по паре (магазин, товар).

    Себестоимость записывается на момент создания заказа: истории цен в системе
    нет (задача B-9), а сумма заказа не должна меняться задним числом вслед
    за карточкой товара.
    """

    __tablename__ = "purchase_order_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("purchase_orders.id", ondelete="CASCADE"), index=True
    )
    store_product_id: Mapped[int] = mapped_column(ForeignKey("store_products.id"), index=True)
    qty: Mapped[int] = mapped_column(Integer, default=0)
    #: Неделя прихода из расчёта закупки — она же неделя поставки.
    arrival_week: Mapped[str] = mapped_column(String(8))
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)
    volume_m3: Mapped[float] = mapped_column(Float, default=0.0)

    order: Mapped[PurchaseOrder] = relationship(back_populates="lines")
    pair: Mapped[StoreProduct] = relationship(lazy="joined")


class WeekPlan(Base):
    """План закрытой недели.

    По макету: «Новый прогноз редактируется в таблице. После закрытия недели
    он фиксируется как её план.»
    """

    __tablename__ = "week_plans"
    __table_args__ = (UniqueConstraint("store_product_id", "week", name="uq_week_plan"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_product_id: Mapped[int] = mapped_column(ForeignKey("store_products.id"), index=True)
    week: Mapped[str] = mapped_column(String(8), index=True)
    plan_units: Mapped[float] = mapped_column(Float, default=0.0)
    frozen_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class ForecastAdjustment(Base):
    """Ручная правка прогноза и DIFF.

    Переживает любой пересчёт — критерий приёмки № 2.
    """

    __tablename__ = "forecast_adjustments"
    __table_args__ = (UniqueConstraint("store_product_id", "week", name="uq_forecast_adjustment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    store_product_id: Mapped[int] = mapped_column(ForeignKey("store_products.id"), index=True)
    week: Mapped[str] = mapped_column(String(8), index=True)
    diff_percent: Mapped[float | None] = mapped_column(Float, default=None)
    manual_forecast: Mapped[float | None] = mapped_column(Float, default=None)
    manual_adapted: Mapped[float | None] = mapped_column(Float, default=None)
    author: Mapped[str | None] = mapped_column(String(64), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())


class AdjustmentLog(Base):
    """Журнал корректировок — требование приёмки по интерфейсу.

    Одна массовая правка порождает сотни записей, поэтому у них есть общий
    ``batch_id``: без него журнал выглядел бы потоком несвязанных изменений,
    в котором не видно, что именно сделал человек.
    """

    __tablename__ = "adjustment_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_product_id: Mapped[int] = mapped_column(ForeignKey("store_products.id"), index=True)
    week: Mapped[str] = mapped_column(String(8), index=True)
    field: Mapped[str] = mapped_column(String(32))
    #: Что породило запись: `manual` или имя массовой операции.
    operation: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    #: Общий идентификатор массовой правки; у поячеечных правок пустой.
    batch_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    old_value: Mapped[float | None] = mapped_column(Float, default=None)
    new_value: Mapped[float | None] = mapped_column(Float, default=None)
    author: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class SyncRun(Base):
    """Прогон синхронизации с маркетплейсом."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, native_enum=False), default=SyncStatus.RUNNING
    )
    facts_upserted: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, default=None)


class User(Base):
    """Пользователь системы. Пароль хранится только как bcrypt-хеш."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(190), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False), default=Role.VIEWER)
    password_hash: Mapped[str] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    #: Когда пароль задали в последний раз. Сессии, выданные раньше этой отметки,
    #: больше не принимаются: смена пароля должна закрывать чужой вход сразу,
    #: а не через двенадцать часов, когда истечёт уже выданный токен.
    #: ``None`` — пароль не меняли с момента заведения, отсекать нечего.
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, default=func.now())

    @property
    def display_name(self) -> str:
        return self.full_name or self.email


class AppSettings(Base):
    """Глобальные настройки расчёта. Одна строка, id = 1."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    #: Последняя закрытая неделя: всё до неё включительно — факт.
    last_closed_week: Mapped[str | None] = mapped_column(String(8), default=None)
    horizon_weeks: Mapped[int] = mapped_column(Integer, default=52)
    history_weeks: Mapped[int] = mapped_column(Integer, default=8)
    over_coverage_weeks: Mapped[float] = mapped_column(Float, default=12.0)
    #: Окно, в котором дефицит считается статусом SHORT.
    short_horizon_weeks: Mapped[int] = mapped_column(Integer, default=8)
    demand_avg_weeks: Mapped[int] = mapped_column(Integer, default=4)
    default_lead_time_days: Mapped[int] = mapped_column(Integer, default=14)
    default_moq: Mapped[int] = mapped_column(Integer, default=0)
    default_pack_size: Mapped[int] = mapped_column(Integer, default=1)
    default_coverage_weeks: Mapped[int] = mapped_column(Integer, default=4)
    default_safety_weeks: Mapped[float] = mapped_column(Float, default=2.0)
    #: Номер версии данных, общих для всех пар: сезонность категорий, привязка
    #: товара к категории, сами настройки расчёта. Отдельная отметка на каждой паре
    #: означала бы обновление всей таблицы при правке одного коэффициента, поэтому
    #: такие изменения считаются одним счётчиком — кэш расчёта сбрасывается целиком.
    calc_epoch: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
