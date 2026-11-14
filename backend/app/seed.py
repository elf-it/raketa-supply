"""Демонстрационные данные.

Наполняет справочники, генерирует фикстуры WB и прогоняет через них настоящую
синхронизацию — ту же, что будет работать с боевым API. Так проверяется весь путь
«источник → единая база → расчётное ядро → интерфейс», а не только расчёты.

    python -m app.seed [--reset]
"""

from __future__ import annotations

import argparse
import json
import math
import random
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import migrations
from .calc import shift_week, week_key, week_start
from .db import SessionLocal, engine
from .integrations.wb import FixtureWbClient, sync_all
from .models import (
    AdjustmentLog,
    AppSettings,
    Category,
    ForecastAdjustment,
    Marketplace,
    Product,
    Role,
    Seasonality,
    Store,
    StoreProduct,
    Supplier,
    Supply,
    SupplyStatus,
    SyncRun,
    User,
    WeekFact,
    WeekPlan,
)

FIXTURES_DIR = Path(__file__).parent / "integrations" / "wb" / "fixtures"
HISTORY_WEEKS = 20

STORES = [
    ("store01", "Магазин 01"),
    ("store02", "Магазин 02"),
    ("store03", "Магазин 03"),
]

CATEGORIES = [
    ("Лампы", "Автотовары", "A"),
    ("Рации", "Электроника", "B"),
    ("Аксессуары", "Автотовары", "C"),
    ("Декор", "Товары для дома", "C"),
]

# (поставщик, lead time по умолчанию, MOQ по умолчанию, его категории).
# Заказ уходит поставщику целиком, поэтому экран закупки группирует по ним; с одним
# поставщиком на весь каталог эта группировка ничего бы не показывала.
SUPPLIERS = [
    ("Фары и оптика", 35, 200, ["Лампы"]),
    ("Радиосвязь-Импорт", 45, 50, ["Рации"]),
    ("Праздник-Опт", 25, 100, ["Аксессуары", "Декор"]),
]

# (артикул, название, категория, себестоимость, цена, м³, lead time, MOQ, PCB)
PRODUCTS = [
    ("2041904540146", "Лампа A10 H7 светодиодная", "Лампы", 420, 1190, 0.0012, 35, 200, 50),
    ("2041904540153", "Лампа A10 H4 светодиодная", "Лампы", 440, 1250, 0.0012, 35, 200, 50),
    ("2041904540160", "Лампа A10 H11 светодиодная", "Лампы", 430, 1210, 0.0012, 35, 200, 50),
    ("881204410021", "Рация Baofeng UV-5R", "Рации", 1850, 3990, 0.0045, 45, 50, 10),
    ("881204410038", "Рация Baofeng UV-82", "Рации", 2100, 4490, 0.0048, 45, 50, 10),
    ("881204410045", "Гарнитура для рации", "Рации", 320, 890, 0.0008, 30, 100, 20),
    ("498277356", "Подставка для 19 шаров", "Аксессуары", 180, 590, 0.0032, 21, 100, 25),
    ("498278609", "Подставка для 25 шаров", "Аксессуары", 210, 690, 0.0038, 21, 100, 25),
    ("187620512", "Утяжелитель для основания арки", "Аксессуары", 95, 340, 0.0021, 21, 200, 50),
    ("187620529", "Держатель для шаров настольный", "Декор", 140, 450, 0.0026, 28, 100, 20),
    ("187620536", "Лента для шаров 50 м", "Декор", 60, 220, 0.0009, 28, 300, 50),
    ("187620543", "Набор зажимов для шаров", "Декор", 75, 260, 0.0011, 28, 300, 50),
]

# (магазин, артикул, базовый потенциал шт/нед)
ASSORTMENT = [
    ("store01", "2041904540146", 120),
    ("store01", "2041904540153", 95),
    ("store01", "2041904540160", 70),
    ("store01", "881204410021", 48),
    ("store01", "881204410038", 32),
    ("store01", "498277356", 30),
    ("store01", "498278609", 42),
    ("store01", "187620512", 50),
    ("store02", "2041904540146", 80),
    ("store02", "881204410021", 60),
    ("store02", "881204410045", 110),
    ("store02", "498277356", 24),
    ("store02", "187620529", 36),
    ("store02", "187620536", 150),
    ("store03", "2041904540153", 40),
    ("store03", "881204410038", 20),
    ("store03", "187620512", 64),
    ("store03", "187620543", 90),
]

#: Сезонность: декабрь выше, январь-февраль ниже. Номер недели → коэффициент.
SEASONALITY_PROFILE = {
    "Лампы": {**{w: 1.25 for w in range(40, 50)}, **{w: 0.8 for w in range(2, 9)}},
    "Рации": {**{w: 1.4 for w in range(46, 53)}, **{w: 0.85 for w in range(1, 7)}},
    "Аксессуары": {**{w: 1.15 for w in range(44, 53)}, **{w: 0.9 for w in range(1, 10)}},
    "Декор": {**{w: 1.6 for w in range(47, 53)}, **{w: 0.7 for w in range(1, 8)}},
}


def reset(session: Session) -> None:
    for model in (
        AdjustmentLog,
        ForecastAdjustment,
        WeekPlan,
        WeekFact,
        Supply,
        SyncRun,
        Seasonality,
        StoreProduct,
        Product,
        Category,
        Supplier,
        Store,
        User,
        AppSettings,
    ):
        session.execute(delete(model))
    session.commit()


#: Демонстрационные учётные записи. Существуют только для локального запуска:
#: пароли лежат в репозитории, значит в реальной установке их обязательно меняют.
#: Боевых пользователей заводит администратор через `POST /api/v1/auth/users`.
DEMO_USERS = [
    ("admin@raketa.local", "Ольга, администратор", Role.ADMIN, "demo-admin-2026"),
    ("editor@raketa.local", "Анна, снабжение", Role.EDITOR, "demo-editor-2026"),
    ("viewer@raketa.local", "Пётр, руководитель", Role.VIEWER, "demo-viewer-2026"),
]


def seed_users(session: Session) -> None:
    from .security import hash_password

    for email, full_name, role, password in DEMO_USERS:
        session.add(
            User(
                email=email,
                full_name=full_name,
                role=role,
                password_hash=hash_password(password),
            )
        )
    session.commit()


def seed_catalog(session: Session) -> None:
    supplier_by_category: dict[str, Supplier] = {}
    for name, lead, moq, category_names in SUPPLIERS:
        supplier = Supplier(name=name, lead_time_days=lead, moq=moq)
        session.add(supplier)
        for category_name in category_names:
            supplier_by_category[category_name] = supplier

    categories: dict[str, Category] = {}
    for name, niche, abc in CATEGORIES:
        category = Category(name=name, niche=niche, abc_class=abc)
        categories[name] = category
        session.add(category)

    stores: dict[str, Store] = {}
    for code, name in STORES:
        store = Store(code=code, name=name, marketplace=Marketplace.WB, cabinet_ref=code)
        stores[code] = store
        session.add(store)

    session.flush()

    products: dict[str, Product] = {}
    for vendor_code, title, category, cost, price, volume, lead, moq, pcb in PRODUCTS:
        product = Product(
            vendor_code=vendor_code,
            title=title,
            category_id=categories[category].id,
            supplier_id=supplier_by_category[category].id,
            cost_price=cost,
            price=price,
            volume_m3=volume,
            lead_time_days=lead,
            moq=moq,
            pack_size=pcb,
            coverage_weeks=4,
            safety_weeks=2.0,
        )
        products[vendor_code] = product
        session.add(product)

    session.flush()

    for store_code, vendor_code, potential in ASSORTMENT:
        product = products[vendor_code]
        session.add(
            StoreProduct(
                store_id=stores[store_code].id,
                product_id=product.id,
                nm_id=f"{store_code[-2:]}{vendor_code}",
                base_potential=potential,
            )
        )

    for name, profile in SEASONALITY_PROFILE.items():
        for iso_week in range(1, 54):
            session.add(
                Seasonality(
                    category_id=categories[name].id,
                    iso_week=iso_week,
                    coefficient=profile.get(iso_week, 1.0),
                )
            )

    session.commit()


def make_fixtures(session: Session, today: date, directory: Path | None = None) -> None:
    """Генерирует правдоподобные дневные выгрузки WB на HISTORY_WEEKS недель назад."""
    directory = directory or FIXTURES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(20261109)

    last_closed = shift_week(week_key(today), -1)
    first_week = shift_week(last_closed, -(HISTORY_WEEKS - 1))
    start = week_start(first_week)
    end = week_start(last_closed) + timedelta(days=6)

    pairs = session.execute(
        select(StoreProduct, Store.code, Product.vendor_code, Product.title, Category.name)
        .join(Store, Store.id == StoreProduct.store_id)
        .join(Product, Product.id == StoreProduct.product_id)
        .outerjoin(Category, Category.id == Product.category_id)
    ).all()

    by_store: dict[str, dict] = {
        code: {"cards": [], "sales": [], "stocks": []} for code, _ in STORES
    }

    for pair, store_code, vendor_code, title, category in pairs:
        bucket = by_store[store_code]
        bucket["cards"].append(
            {
                "nm_id": pair.nm_id,
                "vendor_code": vendor_code,
                "title": title,
                "category": category,
            }
        )

        profile = SEASONALITY_PROFILE.get(category or "", {})
        # Стартовый остаток: примерно 8 недель спроса.
        stock = pair.base_potential * 8
        day = start
        while day <= end:
            iso_week = day.isocalendar()[1]
            coef = profile.get(iso_week, 1.0)
            daily = pair.base_potential * coef / 7.0
            noise = rnd.uniform(0.75, 1.3)
            demand = max(0.0, daily * noise)

            # Поставки примерно раз в 5 недель, в понедельник.
            if day.weekday() == 0 and rnd.random() < 0.2:
                stock += math.ceil(pair.base_potential * 5 / 10) * 10

            sold = min(demand, stock)
            stock = max(0.0, stock - sold)

            if sold > 0:
                bucket["sales"].append(
                    {
                        "nm_id": pair.nm_id,
                        "day": day.isoformat(),
                        "order_units": round(sold * rnd.uniform(1.0, 1.15), 2),
                        "buyout_units": round(sold, 2),
                        "sales_units": round(sold, 2),
                    }
                )
            if day.weekday() == 6:
                bucket["stocks"].append(
                    {
                        "nm_id": pair.nm_id,
                        "day": day.isoformat(),
                        "quantity": round(stock),
                    }
                )
            day += timedelta(days=1)

    for code, payload in by_store.items():
        (directory / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )


def seed_supplies(session: Session, today: date) -> None:
    """Уже заказанные партии — «товары в пути» для расчёта закупки."""
    rnd = random.Random(7)
    current = week_key(today)
    pairs = session.scalars(select(StoreProduct)).all()
    for pair in pairs:
        if rnd.random() < 0.45:
            week = shift_week(current, rnd.randint(1, 6))
            qty = math.ceil(pair.base_potential * rnd.uniform(2, 5) / 10) * 10
            session.add(
                Supply(
                    store_product_id=pair.id,
                    week=week,
                    qty=qty,
                    status=SupplyStatus.IN_TRANSIT,
                    note="демо-поставка",
                )
            )
    session.commit()


def seed_adjustments(session: Session, today: date) -> None:
    """Немного истории правок, чтобы журнал корректировок не был пустым."""
    from .calc import BulkOperation
    from .services.adjustments import bulk_adjust, set_adjustment
    from .services.matrix import MatrixFilters, load_pairs

    pairs = load_pairs(session, MatrixFilters())
    if not pairs:
        return

    current = week_key(today)
    weeks = [shift_week(current, i) for i in range(1, 5)]

    set_adjustment(
        session,
        pairs[0].id,
        weeks[0],
        manual_forecast=round(pairs[0].base_potential * 1.4),
        author="Анна, снабжение",
    )
    set_adjustment(session, pairs[1].id, weeks[1], diff_percent=85, author="Анна, снабжение")

    # Массовая правка: в журнале она должна читаться как одно действие.
    bulk_adjust(
        session,
        pair_ids=[p.id for p in pairs[:5]],
        weeks=weeks[:3],
        operation=BulkOperation.SCALE,
        value=115,
        author="Пётр, руководитель",
        today=today,
    )


def freeze_plans(session: Session, today: date) -> None:
    """План закрытых недель, чтобы KPI «выполнение плана» считался на истории."""
    from .services.matrix import (
        MatrixFilters,
        build_series_inputs,
        load_app_settings,
        load_pairs,
    )

    app_settings = load_app_settings(session)
    last_closed = shift_week(week_key(today), -1)
    weeks = [shift_week(last_closed, -i) for i in range(HISTORY_WEEKS)][::-1]

    pairs = load_pairs(session, MatrixFilters())
    inputs = build_series_inputs(session, pairs, weeks, None)
    rnd = random.Random(42)
    for pair in pairs:
        data = inputs[pair.id]
        for week in weeks:
            seasonal = data.base_potential * data.seasonality.get(week, 1.0)
            # План ставился заранее и не совпадал с фактом в точности.
            session.add(
                WeekPlan(
                    store_product_id=pair.id,
                    week=week,
                    plan_units=round(seasonal * rnd.uniform(0.9, 1.1)),
                )
            )
    app_settings.last_closed_week = last_closed
    app_settings.history_weeks = 8
    app_settings.horizon_weeks = 26
    session.commit()


def main(
    reset_db: bool = False,
    today: date | None = None,
    fixtures_dir: Path | None = None,
) -> None:
    today = today or date.today()
    fixtures_dir = fixtures_dir or FIXTURES_DIR

    # Схему разворачивают миграции, а не create_all: демо-данные должны ложиться
    # ровно на ту же схему, что получит прод.
    url = migrations.engine_url(engine)
    if reset_db:
        migrations.downgrade(url, "base")
    migrations.upgrade(url)

    with SessionLocal() as session:
        if not reset_db:
            reset(session)
        seed_users(session)
        seed_catalog(session)
        make_fixtures(session, today, fixtures_dir)
        reports = sync_all(
            session,
            FixtureWbClient(fixtures_dir),
            days=HISTORY_WEEKS * 7 + 7,
            today=today,
        )
        seed_supplies(session, today)
        freeze_plans(session, today)
        seed_adjustments(session, today)

    for report in reports:
        status = "ok" if report.ok else f"ошибка: {report.error}"
        print(f"{report.store_code}: фактов {report.facts_upserted}, {status}")
    print("Демо-данные загружены.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Загрузка демонстрационных данных")
    parser.add_argument("--reset", action="store_true", help="пересоздать схему БД")
    args = parser.parse_args()
    main(reset_db=args.reset)
