"""Отметки версий данных, по которым кэш расчёта узнаёт об изменениях (задача G-6).

Кэш недельных рядов (`app/services/calc_cache.py`) лежит в памяти процесса, но
решение «годится ли запись» принимается по номерам из базы: у каждой пары
(магазин, товар) свой ``calc_revision``, на всё общее — ``calc_epoch`` в настройках.
Поэтому правка, прошедшая через один воркер, обесценивает кэш у всех остальных:
рассылать им ничего не нужно, они прочитают номер и пересчитают сами.

Номера поднимает слушатель ``before_flush``, а не вызовы по месту. Забытый вызов
дал бы не медленный экран, а молча устаревшие числа — худший вид ошибки в системе
планирования, потому что заказ по ним уже уйдёт поставщику. Слушатель видит всё,
что проходит через сессию, включая код, который напишут позже.

Чего слушатель не видит — запросы, собранные вручную: ``session.execute(delete(...))``
меняет таблицу, не трогая объектов сессии. Такие места поднимают номер сами
через :func:`touch_pairs`; в проекте оно одно — отмена заказа убирает за собой
поставки. Это единственное правило, которое нужно помнить, добавляя Core-DML
по данным расчёта.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import event, update
from sqlalchemy.orm import Session

from .models import (
    AppSettings,
    Category,
    ForecastAdjustment,
    Product,
    Seasonality,
    StoreProduct,
    Supply,
    WeekFact,
    WeekPlan,
)

#: Таблицы, привязанные к паре: изменение касается только её недельного ряда.
PAIR_SCOPED = (WeekFact, Supply, WeekPlan, ForecastAdjustment)

#: Таблицы, общие для всех пар. Сезонность задаётся категории, привязка товара
#: к категории меняет его коэффициенты, настройки — пороги и горизонт расчёта.
#: Отдельная отметка на каждой паре означала бы обновление всей таблицы при правке
#: одного коэффициента, поэтому здесь один общий счётчик.
GLOBAL_SCOPED = (Seasonality, Category, Product, AppSettings)


def touch_pairs(session: Session, pair_ids: Iterable[int | None]) -> int:
    """Поднимает номер версии у перечисленных пар. Возвращает, сколько их было.

    Нужна там, где данные расчёта меняются запросом, а не объектами сессии:
    слушатель ``before_flush`` такое изменение не увидит.
    """
    ids = {int(pair_id) for pair_id in pair_ids if pair_id is not None}
    if not ids:
        return 0
    session.execute(
        update(StoreProduct)
        .where(StoreProduct.id.in_(ids))
        .values(calc_revision=StoreProduct.calc_revision + 1)
        .execution_options(synchronize_session=False)
    )
    return len(ids)


def touch_everything(session: Session) -> bool:
    """Поднимает общий счётчик: вся выборка считается заново.

    Возвращает ``False``, если строки настроек ещё нет — на пустой базе
    сбрасывать нечего.
    """
    settings = session.get(AppSettings, 1)
    if settings is None:
        return False
    settings.calc_epoch = (settings.calc_epoch or 0) + 1
    return True


@event.listens_for(Session, "before_flush")
def _stamp_revisions(session: Session, _flush_context, _instances) -> None:
    """Собирает по сессии, чьи ряды изменились, и поднимает их номера.

    Слушатель висит на классе ``Session``, а не на конкретной фабрике: иначе
    сессия, созданная в обход `app/db.py` (в тестах или скрипте), меняла бы данные,
    не отмечая этого.
    """
    pair_ids: set[int] = set()
    global_touched = False

    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, PAIR_SCOPED):
            pair_ids.add(obj.store_product_id)
        elif isinstance(obj, StoreProduct):
            # У новой пары ещё нет идентификатора, и в кэше её быть не может:
            # она начнёт жизнь с первой версией.
            if obj.id is not None:
                pair_ids.add(obj.id)
        elif isinstance(obj, GLOBAL_SCOPED):
            global_touched = True

    if pair_ids:
        touch_pairs(session, pair_ids)
    if global_touched:
        touch_everything(session)
