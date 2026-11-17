"""Сопоставление SKU: несопоставленные номенклатуры и дубли (задача B-10).

Один физический товар в нескольких кабинетах система уже умеет: ``Product``
общий, а остаток и продажи живут на паре (магазин, товар). **[ДОПУЩЕНИЕ, Q-4]**
Это один из двух возможных ответов — считать такой товар одним SKU с остатками
по кабинетам; второй (разные SKU) потребовал бы другой агрегации, и тогда
поменяется правило сложения, а не этот модуль.

Чего не хватало — не модели, а работы с ней:

* **Несопоставленные номенклатуры.** Загрузка ищет пару по ``nm_id`` внутри
  кабинета и незнакомый номер пропускает. В журнале оставалось «не сопоставлено:
  7», а какие именно — негде; теперь они копятся в таблице, и их можно привязать
  к товару в два действия. Факты по привязанному номеру приедут следующей
  загрузкой: задним числом система их не перезапрашивает.

* **Дубли.** Один и тот же физический товар легко завести дважды под разными
  артикулами — особенно когда каталог приходит из двух кабинетов файлами.
  Тогда спрос, закупка и остатки делятся пополам, а строки выглядят как две
  разные позиции. Здесь — поиск кандидатов по признакам, которые в самом деле
  о чём-то говорят: общий штрихкод, одинаковое название, артикул, отличающийся
  только разделителями.

* **Слияние.** Пары переезжают на целевой товар вместе со всем, что на них
  висит: фактами, поставками, планами, правками, строками заказов. Исходный
  товар не удаляется — он снимается с продажи и остаётся с историей цен, пока
  человек не решит убрать его сам: молча стирать прошлое система не умеет
  нигде, и здесь тоже не будет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import Product, Store, StoreProduct, UnmatchedNmId, WeekFact
from . import catalog as catalog_service


class MatchError(ValueError):
    """Сопоставить нельзя: это не дубль или не хватает данных."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ------------------------------------------------- несопоставленные номенклатуры


def remember_unmatched(session: Session, store_id: int, nm_ids: set[str]) -> int:
    """Запоминает номера, которые загрузка пропустила, и забывает сопоставленные.

    Зовётся из загрузки. Возвращает, сколько номеров сейчас ждёт сопоставления
    по этому кабинету.
    """
    moment = _now()
    rows = {
        row.nm_id: row
        for row in session.scalars(
            select(UnmatchedNmId).where(UnmatchedNmId.store_id == store_id)
        ).all()
    }
    for nm_id in nm_ids:
        row = rows.get(nm_id)
        if row is None:
            session.add(
                UnmatchedNmId(store_id=store_id, nm_id=nm_id, first_seen=moment, last_seen=moment)
            )
        else:
            row.last_seen = moment
            row.times_seen += 1
    # Номер, который загрузка больше не считает незнакомым, в списке не нужен:
    # его либо привязали, либо он исчез из кабинета.
    stale = [nm_id for nm_id in rows if nm_id not in nm_ids]
    if stale:
        session.execute(
            delete(UnmatchedNmId).where(
                UnmatchedNmId.store_id == store_id, UnmatchedNmId.nm_id.in_(stale)
            )
        )
    session.commit()
    return len(nm_ids)


@dataclass(frozen=True)
class Unmatched:
    store_id: int
    store_code: str
    store_name: str
    nm_id: str
    first_seen: datetime
    last_seen: datetime
    times_seen: int


def unmatched(session: Session, store_id: int | None = None) -> list[Unmatched]:
    """Номера, которые загрузка не смогла положить ни на одну пару."""
    stmt = (
        select(UnmatchedNmId, Store)
        .join(Store, Store.id == UnmatchedNmId.store_id)
        .order_by(UnmatchedNmId.times_seen.desc(), UnmatchedNmId.nm_id)
    )
    if store_id is not None:
        stmt = stmt.where(UnmatchedNmId.store_id == store_id)
    return [
        Unmatched(
            store_id=store.id,
            store_code=store.code,
            store_name=store.name,
            nm_id=row.nm_id,
            first_seen=row.first_seen,
            last_seen=row.last_seen,
            times_seen=row.times_seen,
        )
        for row, store in session.execute(stmt).all()
    ]


def bind(session: Session, *, store_id: int, nm_id: str, product_id: int) -> StoreProduct:
    """Привязывает номенклатуру кабинета к товару.

    Если товар в этом кабинете уже заведён — номер проставляется существующей
    паре, иначе пара заводится. И то и другое делает сервис справочников,
    поэтому действуют его правила: номер остаётся уникальным внутри кабинета.
    """
    nm_id = (nm_id or "").strip()
    if not nm_id:
        raise MatchError("не передан номер номенклатуры")
    product = session.get(Product, product_id)
    if product is None:
        raise MatchError("товар не найден")
    if session.get(Store, store_id) is None:
        raise MatchError("кабинет не найден")

    pair = session.scalar(
        select(StoreProduct).where(
            StoreProduct.store_id == store_id, StoreProduct.product_id == product_id
        )
    )
    if pair is None:
        pair = catalog_service.add_pair(session, product=product, store_id=store_id, nm_id=nm_id)
    else:
        catalog_service.update_pair(session, pair, nm_id=nm_id)

    session.execute(
        delete(UnmatchedNmId).where(
            UnmatchedNmId.store_id == store_id, UnmatchedNmId.nm_id == nm_id
        )
    )
    session.commit()
    return pair


# ------------------------------------------------------------------------ дубли


def _key(value: str | None) -> str:
    """Нормализованный ключ сравнения: без разделителей и регистра."""
    return re.sub(r"[^0-9a-zа-яё]+", "", (value or "").lower())


@dataclass(frozen=True)
class DuplicateHint:
    """Пара товаров, похожих настолько, что стоит посмотреть глазами."""

    reason: str
    left_id: int
    left_code: str
    left_title: str
    left_pairs: int
    right_id: int
    right_code: str
    right_title: str
    right_pairs: int


def duplicate_candidates(session: Session, limit: int = 50) -> list[DuplicateHint]:
    """Кандидаты на слияние. Решает человек: это подсказка, а не вывод."""
    products = list(session.scalars(select(Product).order_by(Product.vendor_code)).all())
    pair_counts = dict(
        session.execute(
            select(StoreProduct.product_id, func.count(StoreProduct.id)).group_by(
                StoreProduct.product_id
            )
        ).all()
    )
    barcodes: dict[str, list[int]] = {}
    for product_id, barcode in session.execute(
        select(StoreProduct.product_id, StoreProduct.barcode).where(
            StoreProduct.barcode.is_not(None)
        )
    ).all():
        barcodes.setdefault(_key(barcode), []).append(product_id)

    by_id = {product.id: product for product in products}
    found: dict[tuple[int, int], str] = {}

    def remember(left: int, right: int, reason: str) -> None:
        if left == right:
            return
        key = (min(left, right), max(left, right))
        found.setdefault(key, reason)

    # Штрихкод — самый сильный признак: это номер самого товара, а не наш.
    for ids in barcodes.values():
        unique = sorted(set(ids))
        for left in unique:
            for right in unique:
                if left < right:
                    remember(left, right, "общий штрихкод")

    by_title: dict[str, list[int]] = {}
    by_code: dict[str, list[int]] = {}
    for product in products:
        by_title.setdefault(_key(product.title), []).append(product.id)
        by_code.setdefault(_key(product.vendor_code), []).append(product.id)
    for ids in by_title.values():
        for left in ids:
            for right in ids:
                if left < right:
                    remember(left, right, "одинаковое название")
    for ids in by_code.values():
        for left in ids:
            for right in ids:
                if left < right:
                    remember(left, right, "артикул отличается только разделителями")

    hints = []
    for (left, right), reason in found.items():
        first, second = by_id[left], by_id[right]
        # Товар без кабинетов сливать нечем и незачем: чаще всего это как раз
        # тот, которого уже слили, и подсказка о нём осталась бы навсегда.
        if not pair_counts.get(first.id) or not pair_counts.get(second.id):
            continue
        hints.append(
            DuplicateHint(
                reason=reason,
                left_id=first.id,
                left_code=first.vendor_code,
                left_title=first.title,
                left_pairs=pair_counts.get(first.id, 0),
                right_id=second.id,
                right_code=second.vendor_code,
                right_title=second.title,
                right_pairs=pair_counts.get(second.id, 0),
            )
        )
    hints.sort(key=lambda hint: (hint.reason, hint.left_code))
    return hints[:limit]


@dataclass
class MergeResult:
    moved_pairs: int
    moved_facts: int
    source_id: int
    target_id: int


def merge(session: Session, *, source: Product, target: Product) -> MergeResult:
    """Переносит пары исходного товара на целевой.

    Факты, поставки, планы, правки и строки заказов висят на паре и переезжают
    вместе с ней — ничего не теряется и ничего не пересчитывается задним числом.

    Отказ, если в одном кабинете заведены оба товара: это не дубль, а две
    позиции, и какая из них «настоящая», система решать не должна — слияние
    потеряло бы продажи одной из них.
    """
    if source.id == target.id:
        raise MatchError("нельзя слить товар сам с собой")

    source_stores = {
        pair.store_id: pair
        for pair in session.scalars(
            select(StoreProduct).where(StoreProduct.product_id == source.id)
        ).all()
    }
    target_stores = {
        pair.store_id
        for pair in session.scalars(
            select(StoreProduct).where(StoreProduct.product_id == target.id)
        ).all()
    }
    if not source_stores:
        raise MatchError(f"у товара {source.vendor_code} нет кабинетов: переносить нечего")

    clash = sorted(source_stores.keys() & target_stores)
    if clash:
        names = ", ".join(
            store.name for store in session.scalars(select(Store).where(Store.id.in_(clash))).all()
        )
        raise MatchError(
            f"обе позиции заведены в одном кабинете ({names}). Это не дубль, "
            "а две разные позиции: слияние потеряло бы продажи одной из них. "
            "Отключите лишнюю пару или перенесите её вручную"
        )

    moved_facts = 0
    for pair in source_stores.values():
        moved_facts += (
            session.scalar(
                select(func.count())
                .select_from(WeekFact)
                .where(WeekFact.store_product_id == pair.id)
            )
            or 0
        )
        pair.product_id = target.id
    # Товар без кабинетов больше нигде не считается; снимаем его с продажи,
    # но не удаляем: у него своя история цен, и решение убрать её — человека.
    source.is_active = False
    session.commit()
    return MergeResult(
        moved_pairs=len(source_stores),
        moved_facts=moved_facts,
        source_id=source.id,
        target_id=target.id,
    )
