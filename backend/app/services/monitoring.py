"""Состояние загрузки данных: что показывать человеку и чем мерить снаружи (G-4).

Сбой синхронизации — самая дорогая из незаметных поломок: экран продолжает
показывать числа, они просто не те. Поэтому состояние считается в одном месте
и отдаётся двумя дорогами: наружу — готовностью (`/health/ready`), по которой
алертит внешний мониторинг, и внутрь — плашкой в интерфейсе, чтобы человек
увидел отставание раньше, чем соберёт по этим числам заказ.

Отставание считается по последнему **завершённому** прогону каждого активного
кабинета:

* ``failed`` — последний прогон упал;
* ``stale`` — последний удачный прогон старше порога (``SYNC_STALE_HOURS``);
* ``never`` — кабинет заведён, но ни разу не загружался;
* ``ok`` — свежие данные.

``never`` не делает систему «нездоровой» намеренно: на свежем развёртывании
кабинеты заводят раньше первой загрузки, и готовность, падающая до первой
синхронизации, заставила бы дежурного гасить алерт вместо работы.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Store, SyncRun, SyncStatus

OK = "ok"
FAILED = "failed"
STALE = "stale"
NEVER = "never"

#: Состояния, при которых систему нельзя считать здоровой.
BAD = (FAILED, STALE)


@dataclass(frozen=True)
class StoreSync:
    store_id: int
    store_code: str
    store_name: str
    status: str
    finished_at: datetime | None = None
    facts_upserted: int = 0
    message: str | None = None


@dataclass
class SyncHealth:
    stale_after_hours: int
    stores: list[StoreSync] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "degraded" if any(row.status in BAD for row in self.stores) else OK

    @property
    def failed(self) -> list[StoreSync]:
        return [row for row in self.stores if row.status == FAILED]

    @property
    def stale(self) -> list[StoreSync]:
        return [row for row in self.stores if row.status == STALE]


def _last_finished(session: Session, store_id: int) -> SyncRun | None:
    """Последний завершённый прогон кабинета — удачный или нет.

    Незавершённый не берётся: пока прогон идёт, состояние определяет предыдущий,
    иначе каждая загрузка на минуту делала бы кабинет «без данных».
    """
    return session.scalars(
        select(SyncRun)
        .where(SyncRun.store_id == store_id, SyncRun.finished_at.is_not(None))
        .order_by(SyncRun.finished_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).first()


def sync_health(
    session: Session, now: datetime | None = None, stale_hours: int | None = None
) -> SyncHealth:
    moment = now or datetime.now()
    hours = stale_hours if stale_hours is not None else get_settings().sync_stale_hours
    health = SyncHealth(stale_after_hours=hours)

    stores = session.scalars(
        select(Store).where(Store.is_active.is_(True)).order_by(Store.code)
    ).all()
    for store in stores:
        run = _last_finished(session, store.id)
        if run is None:
            health.stores.append(
                StoreSync(
                    store_id=store.id,
                    store_code=store.code,
                    store_name=store.name,
                    status=NEVER,
                )
            )
            continue
        if run.status is SyncStatus.FAILED:
            status = FAILED
        elif run.finished_at is not None and run.finished_at < moment - timedelta(hours=hours):
            status = STALE
        else:
            status = OK
        health.stores.append(
            StoreSync(
                store_id=store.id,
                store_code=store.code,
                store_name=store.name,
                status=status,
                finished_at=run.finished_at,
                facts_upserted=run.facts_upserted,
                message=run.message,
            )
        )
    return health
