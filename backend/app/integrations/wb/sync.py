"""Нормализация данных WB в недельные факты.

Загрузка идемпотентна: ключ `(store_product_id, week)`, повторный прогон того же
периода не плодит дубли. Данные разных кабинетов не смешиваются — каждая строка
привязана к паре (магазин, товар), а пара принадлежит ровно одному кабинету
(критерий приёмки № 1).
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...calc import week_key
from ...models import Store, StoreProduct, SyncRun, SyncStatus, WeekFact
from ...observability import event
from ...services.matching import remember_unmatched
from .client import WbClient, msk_today
from .limits import Traffic


def _now() -> datetime:
    """Время окончания прогона — в UTC без зоны, как хранит база.

    ``datetime.now()`` вернул бы местное время, и на машине с часовым поясом
    конец прогона оказывался бы «раньше» его начала, которое ставит сама база
    (``func.now()``). На таких отметках считается отставание загрузки.
    """
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class SyncReport:
    store_code: str
    facts_upserted: int = 0
    unknown_nm_ids: list[str] | None = None
    error: str | None = None
    #: Чего стоил прогон: запросы, повторы, страницы, повторные строки
    #: (задача E-5). У фикстурного клиента ничего этого нет — ``None``.
    traffic: Traffic | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _take_traffic(client: WbClient) -> Traffic | None:
    """Счётчики клиента, если он их ведёт.

    Через ``getattr``, а не через протокол: фикстурному клиенту и подставным
    клиентам в тестах рассказывать не о чем.
    """
    take = getattr(client, "take_traffic", None)
    return take() if callable(take) else None


def _pair_index(session: Session, store_id: int) -> dict[str, int]:
    """nm_id → store_product_id в пределах одного кабинета."""
    rows = session.execute(
        select(StoreProduct.nm_id, StoreProduct.id).where(StoreProduct.store_id == store_id)
    ).all()
    return {str(nm_id): pair_id for nm_id, pair_id in rows if nm_id}


def sync_store(
    session: Session,
    store: Store,
    client: WbClient,
    date_from: date,
) -> SyncReport:
    """Загружает продажи и остатки одного кабинета в недельные факты."""
    run = SyncRun(store_id=store.id, status=SyncStatus.RUNNING)
    session.add(run)
    session.commit()

    report = SyncReport(store_code=store.code, unknown_nm_ids=[])
    try:
        pairs = _pair_index(session, store.id)
        sales = client.fetch_sales(store.code, date_from)
        # Заказы — отдельный метод WB со своим лимитом (задача E-8): «Продажи»
        # отдают продажи и возвраты, а в расчёте спроса участвуют и заказы.
        orders = client.fetch_orders(store.code, date_from)
        stocks = client.fetch_stocks(store.code, date_from)

        weekly: dict[tuple[int, str], dict[str, float]] = defaultdict(
            lambda: {"order_units": 0.0, "buyout_units": 0.0, "sales_units": 0.0}
        )
        unknown: set[str] = set()

        for row in sales:
            pair_id = pairs.get(row.nm_id)
            if pair_id is None:
                unknown.add(row.nm_id)
                continue
            bucket = weekly[(pair_id, week_key(row.day))]
            bucket["order_units"] += row.order_units
            bucket["buyout_units"] += row.buyout_units
            bucket["sales_units"] += row.sales_units

        for row in orders:
            pair_id = pairs.get(row.nm_id)
            if pair_id is None:
                unknown.add(row.nm_id)
                continue
            weekly[(pair_id, week_key(row.day))]["order_units"] += row.order_units

        # Остаток недели — последнее известное значение внутри недели.
        stock_end: dict[tuple[int, str], tuple[date, float]] = {}
        for row in stocks:
            pair_id = pairs.get(row.nm_id)
            if pair_id is None:
                unknown.add(row.nm_id)
                continue
            key = (pair_id, week_key(row.day))
            current = stock_end.get(key)
            if current is None or row.day >= current[0]:
                stock_end[key] = (row.day, row.quantity)

        touched = set(weekly) | set(stock_end)
        existing = {
            (f.store_product_id, f.week): f
            for f in session.scalars(
                select(WeekFact).where(
                    WeekFact.store_product_id.in_({k[0] for k in touched} or {-1})
                )
            ).all()
        }

        for key in sorted(touched):
            pair_id, week = key
            values = weekly.get(key)
            fact = existing.get(key)
            if fact is None:
                fact = WeekFact(store_product_id=pair_id, week=week)
                session.add(fact)
            if values is not None:
                fact.order_units = values["order_units"]
                fact.buyout_units = values["buyout_units"]
                fact.sales_units = values["sales_units"]
            if key in stock_end:
                fact.stock_end_units = stock_end[key][1]
            fact.source = "wb"
            report.facts_upserted += 1

        report.unknown_nm_ids = sorted(unknown)
        report.traffic = _take_traffic(client)
        run.status = SyncStatus.SUCCESS
        run.facts_upserted = report.facts_upserted
        notes = []
        if unknown:
            notes.append(f"не сопоставлено номенклатур: {len(unknown)}")
        traffic = report.traffic
        if traffic is not None and traffic.truncated:
            # Записанные факты верны, но картина неполная — и выглядеть полной
            # она не должна: по неполным продажам посчитается заниженный спрос
            # и недозаказ. Поэтому прогон неудачный, хотя все запросы прошли:
            # так об этом скажут и готовность, и алерт (задачи G-4, G-16).
            run.status = SyncStatus.FAILED
            report.error = (
                "данные загружены не полностью: предел страниц достигнут "
                "раньше, чем кончились данные. Поднимите WB_MAX_PAGES "
                "или грузите меньшую глубину"
            )
            notes.append(report.error)
        if traffic is not None and traffic.note():
            notes.append(traffic.note())
        if notes:
            run.message = "; ".join(notes)
        run.finished_at = _now()
        session.commit()
        # Номера, которые некуда положить, запоминаются после записи фактов:
        # иначе в журнале остаётся «не сопоставлено: 7», и какие именно —
        # узнать негде (задача B-10). Отдельной записью, уже после коммита:
        # упавший прогон не должен оставлять за собой список.
        remember_unmatched(session, store.id, unknown)
        event(
            "кабинет загружен не полностью" if report.error else "кабинет загружен",
            level=logging.WARNING if report.error else logging.INFO,
            store=store.code,
            facts=report.facts_upserted,
            unknown=len(unknown),
            requests=traffic.requests if traffic else 0,
            retries=traffic.retries if traffic else 0,
            pages=traffic.pages if traffic else 0,
            duplicates=traffic.duplicates if traffic else 0,
            waited=f"{traffic.waited_seconds:.0f}" if traffic else "0",
        )
    except Exception as exc:  # noqa: BLE001 — любая ошибка должна попасть в журнал
        session.rollback()
        report.error = f"{type(exc).__name__}: {exc}"
        # Счётчики забираются и у упавшего прогона: по ним видно, упал он сразу
        # или после трёх повторов и трёх минут ожидания лимита.
        report.traffic = _take_traffic(client)
        run = session.get(SyncRun, run.id)
        if run is not None:
            run.status = SyncStatus.FAILED
            note = report.traffic.note() if report.traffic else ""
            run.message = f"{report.error}; {note}" if note else report.error
            run.finished_at = _now()
            session.commit()
        # В журнал синхронизаций ошибка уже записана, но туда смотрят, когда
        # что-то заподозрили. В лог она попадает сразу и с трассировкой.
        event("кабинет не загрузился", level=logging.ERROR, store=store.code, exception=True)
    return report


def sync_all(
    session: Session,
    client: WbClient,
    days: int = 90,
    today: date | None = None,
) -> list[SyncReport]:
    """Синхронизирует все активные кабинеты WB одновременно.

    Глубина отсчитывается от **московской** даты: ``dateFrom`` у WB задаётся
    в московском времени, и на машине в другом поясе местная дата может
    отличаться от той, которую имеет в виду API (задача E-8).
    """
    date_from = (today or msk_today()) - timedelta(days=days)
    stores = session.scalars(select(Store).where(Store.is_active.is_(True))).all()
    return [sync_store(session, store, client, date_from) for store in stores]
