"""Загрузка данных WB одной командой — для планировщика (задача E-6).

    python -m app.sync --days 14

До этой задачи загрузку запускал человек кнопкой на экране, и свежесть данных
держалась на том, что кто-то помнит про неё каждое утро. Внешнему планировщику
при этом нечем было представиться: доступ к API — только по сессионной куке,
а служебных токенов в системе нет.

Решения и почему так:

* **Планировщик снаружи, а не внутри приложения.** Расписание держит тот, кто
  запускает процесс: cron, systemd timer, планировщик заданий Windows. Фоновый
  поток внутри приложения на нескольких воркерах дал бы столько же
  одновременных загрузок, сколько воркеров, а при перезапуске — ещё одну сверх
  расписания. Та же причина, по которой приложение не применяет миграции само.

* **Команда ходит в базу напрямую.** Ей не нужен ни токен, ни сессия: это тот
  же код, что и за ручкой на экране, запущенный в своём процессе. Заводить ради
  планировщика служебную учётную запись с вечным паролем — значит заводить
  вечный пароль.

* **Две загрузки разом не идут.** Если предыдущий прогон ещё не закончился,
  команда не начинает новый: два прогона по одному кабинету пишут одни и те же
  факты и соревнуются за строки. Брошенный прогон (процесс убили) узнаётся
  по возрасту и в расчёт не берётся — иначе одна смерть процесса остановила бы
  расписание навсегда.

* **Код возврата говорит планировщику, что случилось.** ``0`` — загрузилось,
  ``1`` — хотя бы один кабинет упал, ``2`` — предыдущая загрузка ещё идёт.
  Так cron или systemd присылают письмо ровно тогда, когда что-то не так,
  даже если доставка алертов (задача G-16) не настроена.

* **Об упавшем кабинете команда сообщает сама** — настроенными каналами
  доставки, в конце прогона. Отдельный прогон ``python -m app.alerts`` нужен
  для другого случая: когда сама загрузка не запускалась.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from .db import SessionLocal, engine
from .integrations.wb import make_client, policy_from, sync_all
from .migrations import assert_current
from .models import SyncRun, SyncStatus
from .observability import configure_logging, event

#: Через сколько минут незавершённый прогон считается брошенным. Загрузка трёх
#: кабинетов идёт секунды; полчаса — это «процесс точно умер», а не «ещё пишет».
DEFAULT_LOCK_MINUTES = 30

OK = 0
FAILED = 1
BUSY = 2


def _now() -> datetime:
    """UTC без зоны — как хранит база (``func.now()``)."""
    return datetime.now(UTC).replace(tzinfo=None)


def unfinished_run(session, lock_minutes: int, now: datetime | None = None) -> SyncRun | None:
    """Прогон, который ещё идёт. Брошенный (слишком старый) — не считается."""
    moment = now or _now()
    run = session.scalars(
        select(SyncRun)
        .where(SyncRun.finished_at.is_(None))
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(1)
    ).first()
    if run is None:
        return None
    started = run.started_at or moment
    return run if started > moment - timedelta(minutes=lock_minutes) else None


def close_abandoned(session, lock_minutes: int, now: datetime | None = None) -> int:
    """Закрывает брошенные прогоны, чтобы они не висели в журнале вечно.

    Такой прогон помечается неудачей — он ею и был: процесс умер, не дописав.
    На свежесть данных это не влияет, её решает следующий удачный прогон.
    """
    moment = now or _now()
    rows = session.scalars(
        select(SyncRun).where(SyncRun.finished_at.is_(None), SyncRun.started_at.is_not(None))
    ).all()
    closed = 0
    for run in rows:
        if run.started_at > moment - timedelta(minutes=lock_minutes):
            continue
        run.status = SyncStatus.FAILED
        run.message = "прогон не завершился: процесс остановлен"
        run.finished_at = moment
        closed += 1
    if closed:
        session.commit()
    return closed


def main(days: int = 90, lock_minutes: int = DEFAULT_LOCK_MINUTES) -> int:
    """Загружает все активные кабинеты. Возвращает код возврата процесса."""
    from .config import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    # Схему команда не правит — как и приложение: работать на устаревшей базе
    # хуже, чем не запуститься.
    assert_current(engine)

    with SessionLocal() as session:
        busy = unfinished_run(session, lock_minutes)
        if busy is not None:
            event("загрузка пропущена: предыдущая ещё идёт", level=logging.WARNING, run=busy.id)
            print(
                f"Предыдущая загрузка ещё идёт (прогон № {busy.id} от "
                f"{busy.started_at:%d.%m.%Y %H:%M} UTC). Новая не начата."
            )
            return BUSY

        closed = close_abandoned(session, lock_minutes)
        if closed:
            print(f"Закрыто брошенных прогонов: {closed}.")

        client = make_client(
            settings.wb_client,
            settings.wb_token_map,
            settings.wb_api_base,
            Path(settings.wb_fixtures_dir) if settings.wb_fixtures_dir else None,
            policy_from(settings),
            settings.wb_stocks_source,
        )
        reports = sync_all(session, client, days=days)

    if not reports:
        print("Активных кабинетов нет — загружать нечего.")
        return OK

    for report in reports:
        note = report.traffic.note() if report.traffic else ""
        if report.ok:
            unknown = len(report.unknown_nm_ids or [])
            tail = f", не сопоставлено номенклатур: {unknown}" if unknown else ""
            if note:
                tail += f" ({note})"
            print(f"{report.store_code}: фактов {report.facts_upserted}{tail}")
        else:
            tail = f" ({note})" if note else ""
            print(f"{report.store_code}: ошибка — {report.error}{tail}", file=sys.stderr)

    failed = [report.store_code for report in reports if not report.ok]
    event(
        "загрузка по расписанию",
        level=logging.WARNING if failed else logging.INFO,
        stores=len(reports),
        facts=sum(report.facts_upserted for report in reports),
        failed=",".join(failed) or "-",
        days=days,
        source=settings.wb_client,
    )
    notify(settings)
    return FAILED if failed else OK


def notify(settings) -> None:
    """Сообщить о сбое сразу, не дожидаясь отдельного прогона алертов (G-16).

    Доставка не влияет на код возврата загрузки: планировщику важно, загрузились
    ли данные, а не дошло ли письмо. О недоставленном алерте говорит лог
    и код возврата ``python -m app.alerts``.

    Своя сессия: предыдущая закрыта вместе с загрузкой, а правила алертов
    читают журнал прогонов — в том числе только что записанный.
    """
    try:
        from .services.alerts import run_alerts
        from .services.notify import make_channels

        channels = make_channels(settings)
        if not channels:
            return
        with SessionLocal() as session:
            report = run_alerts(
                session,
                channels,
                repeat_hours=settings.alert_repeat_hours,
                prefix=settings.alert_subject_prefix,
            )
        for line in report.failures:
            print(f"Алерт не доставлен — {line}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — доставка алертов не должна валить загрузку
        event("алерты не отправлены", level=logging.ERROR, exception=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Загрузка данных WB во все активные кабинеты",
        epilog="Коды возврата: 0 — загрузилось, 1 — кабинет упал, 2 — предыдущая ещё идёт.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="глубина загрузки в днях; ежедневному прогону хватает двух недель",
    )
    parser.add_argument(
        "--lock-minutes",
        type=int,
        default=DEFAULT_LOCK_MINUTES,
        help="через сколько минут незавершённый прогон считается брошенным",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(main(days=args.days, lock_minutes=args.lock_minutes))
    except RuntimeError as error:
        # Проверка схемы — человеку в stderr, а не трассировкой.
        print(str(error), file=sys.stderr)
        raise SystemExit(FAILED) from None
