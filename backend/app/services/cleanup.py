"""Чистка журналов: что можно забыть и чего забывать нельзя (задача G-19).

Две таблицы растут без предела. `sync_runs` — строка на каждый прогон каждого
кабинета: при загрузке раз в час и трёх кабинетах это семьдесят тысяч строк
в год. `adjustment_log` — строка на каждую правленную ячейку, а одна массовая
правка доходит до 20 000 ячеек. Счётчик попыток входа и несопоставленные номера
убирают за собой сами, эти — нет.

Решения и почему так:

* **Последний завершённый прогон кабинета не удаляется никогда.** По нему
  считается отставание загрузки (задача G-4): кабинет без прогонов выглядит
  «ни разу не загружался», а такое состояние намеренно не делает систему
  нездоровой — значит, чистка молча погасила бы алерт о протухших данных.
  Это главная ловушка задачи: удаление строки журнала ломает не журнал,
  а сторожа.

* **Журнал корректировок по умолчанию не трогается.** Это доказательство:
  кто поставил число и когда. Срок хранения такому назначает тот, кто отвечает
  за данные, а не программа при установке, поэтому чистка журнала включается
  отдельным ключом и отдельным сроком.

* **Сначала показать, потом удалить.** У команды есть прогон вхолостую,
  и он идёт первым в документации: удаление в базе не отменяется кнопкой.

* **Удаление идёт запросом, а не через сессию.** Строки журнала никого
  не пересчитывают, и поднимать из-за них в память сотни тысяч сущностей
  незачем. Номера версий расчёта такие удаления не трогают: ни прогон
  синхронизации, ни запись журнала в расчёт не входят.

* **Отозванные сессии чистятся без ключа и без срока.** Строка об отзыве
  (задача G-20) нужна ровно до ``exp`` своего токена: после него токен
  отвергает подпись. Держать дольше бессмысленно, удалять раньше — значит
  вернуть отозванный токен в работу. Выбирать здесь нечего, поэтому
  настройки нет.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import AdjustmentLog, RevokedSession, Store, SyncRun

#: Сколько хранить прогоны синхронизации. Месяца хватает на разбор любого
#: «почему в прошлый вторник числа были не те»: дальше смотрят в лог.
DEFAULT_RUN_DAYS = 90
#: Журнал корректировок по умолчанию не чистится вовсе — это доказательство.
DEFAULT_JOURNAL_DAYS = 0


def _now() -> datetime:
    """UTC без зоны — как хранит база."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class CleanupPlan:
    """Что будет удалено. Одинаково считается и для прогона вхолостую."""

    runs: int = 0
    journal: int = 0
    #: Просроченные отзывы сессий (задача G-20) — срока хранения у них нет.
    sessions: int = 0
    kept_runs: list[str] = None  # type: ignore[assignment]
    run_cutoff: datetime | None = None
    journal_cutoff: datetime | None = None

    def __post_init__(self) -> None:
        if self.kept_runs is None:
            self.kept_runs = []

    @property
    def total(self) -> int:
        return self.runs + self.journal + self.sessions


def protected_run_ids(session: Session) -> dict[int, int]:
    """Последний завершённый прогон каждого кабинета: код магазина → id прогона.

    Эти строки не удаляются ни при каком сроке хранения — на них держится
    проверка свежести данных.
    """
    protected: dict[int, int] = {}
    stores = session.scalars(select(Store.id)).all()
    for store_id in stores:
        run_id = session.scalar(
            select(SyncRun.id)
            .where(SyncRun.store_id == store_id, SyncRun.finished_at.is_not(None))
            .order_by(SyncRun.finished_at.desc(), SyncRun.id.desc())
            .limit(1)
        )
        if run_id is not None:
            protected[store_id] = run_id
    return protected


def plan_cleanup(
    session: Session,
    run_days: int = DEFAULT_RUN_DAYS,
    journal_days: int = DEFAULT_JOURNAL_DAYS,
    now: datetime | None = None,
) -> CleanupPlan:
    """Считает, что подлежит удалению. Ничего не меняет."""
    moment = now or _now()
    plan = CleanupPlan()

    if run_days > 0:
        plan.run_cutoff = moment - timedelta(days=run_days)
        keep = set(protected_run_ids(session).values())
        plan.runs = (
            session.scalar(
                select(func.count())
                .select_from(SyncRun)
                .where(SyncRun.started_at < plan.run_cutoff, SyncRun.id.notin_(keep or {-1}))
            )
            or 0
        )
        codes = session.scalars(
            select(Store.code).where(Store.id.in_(protected_run_ids(session).keys()))
        ).all()
        plan.kept_runs = sorted(codes)

    if journal_days > 0:
        plan.journal_cutoff = moment - timedelta(days=journal_days)
        plan.journal = (
            session.scalar(
                select(func.count())
                .select_from(AdjustmentLog)
                .where(AdjustmentLog.created_at < plan.journal_cutoff)
            )
            or 0
        )

    plan.sessions = (
        session.scalar(
            select(func.count())
            .select_from(RevokedSession)
            .where(RevokedSession.expires_at < moment)
        )
        or 0
    )
    return plan


def run_cleanup(
    session: Session,
    run_days: int = DEFAULT_RUN_DAYS,
    journal_days: int = DEFAULT_JOURNAL_DAYS,
    now: datetime | None = None,
) -> CleanupPlan:
    """Удаляет то, что посчитал план. Возвращает его же — с числами удалённого."""
    # Момент фиксируется один раз на весь прогон: посчитай план по одному
    # времени, а удаляй по другому — и удалённых окажется больше, чем обещал
    # прогон вхолостую.
    moment = now or _now()
    plan = plan_cleanup(session, run_days, journal_days, moment)

    if plan.run_cutoff is not None and plan.runs:
        keep = set(protected_run_ids(session).values())
        session.execute(
            delete(SyncRun).where(
                SyncRun.started_at < plan.run_cutoff, SyncRun.id.notin_(keep or {-1})
            )
        )
    if plan.journal_cutoff is not None and plan.journal:
        session.execute(delete(AdjustmentLog).where(AdjustmentLog.created_at < plan.journal_cutoff))
    if plan.sessions:
        session.execute(delete(RevokedSession).where(RevokedSession.expires_at < moment))
    if plan.total:
        session.commit()
    return plan
