"""Отправка алертов одной командой — для планировщика (задача G-16).

    python -m app.alerts              — проверить состояние и сообщить о новом
    python -m app.alerts --dry-run    — показать, что ушло бы, и ничего не слать
    python -m app.alerts --test       — проверить сами каналы доставки

Решения и почему так:

* **Команда, а не фоновый поток.** Та же причина, по которой снаружи живёт
  и расписание загрузки (задача E-6): поток внутри приложения на нескольких
  воркерах дал бы столько же отправок, сколько воркеров. Запускать её стоит
  тем же планировщиком и чаще загрузки — отставание данных нужно замечать
  между прогонами, а не только после них.

* **Загрузка сообщает о себе сама.** ``python -m app.sync`` в конце зовёт
  те же правила: о упавшем кабинете дежурный узнаёт сразу, а не в следующий
  прогон алертов. Эта команда остаётся нужна для второго случая — когда сама
  загрузка не запускалась.

* **``--test`` отправляет, ничего не проверяя.** Канал, в который ни разу
  не отправляли, — это предположение о канале: пароль приложения, забытый
  порт, вебхук, отозванный полгода назад. Проверка состояние не трогает:
  ни отметок, ни повторов. Пульс наружу проверяется вместе с каналами.

* **Пульс наружу (задача G-18).** После удачного прогона команда дёргает
  ``HEARTBEAT_URL`` — адрес сторожа снаружи (healthchecks.io, push-проверка
  Uptime Kuma, Dead Man's Snitch). Это единственный способ узнать, что система
  мертва: выключенная машина не отправляет алертов, и о собственной смерти
  сообщить нельзя — зато можно делать обратное, пока жив. Если прогон
  неудачный, пульса нет намеренно: молчание и есть сигнал.

* **Код возврата.** ``0`` — сообщать нечего или всё отправлено, ``1`` — было
  что сообщить, и ни один канал не принял. Так cron пришлёт письмо ровно
  тогда, когда алерт не ушёл, — алертом об алертах.
"""

from __future__ import annotations

import argparse
import logging
import platform
import sys
from datetime import UTC, datetime

from .db import SessionLocal, engine
from .migrations import assert_current
from .observability import configure_logging, event
from .services.alerts import RECOVERED, Report, deliver, run_alerts
from .services.notify import (
    AlertConfigError,
    DeliveryError,
    Note,
    make_channels,
    mask_url,
    send_heartbeat,
)

OK = 0
FAILED = 1


def _print_channels(report: Report) -> None:
    if not report.channels:
        print(
            "Доставка алертов не настроена: ни SMTP_HOST, ни ALERT_WEBHOOK_URL.\n"
            "О сбоях сообщают только готовность /health/ready и плашка в интерфейсе."
        )
        return
    print("Каналы доставки:")
    for line in report.channels:
        print(f"  {line}")


def main(dry_run: bool = False, test: bool = False, repeat_hours: int | None = None) -> int:
    from .config import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)

    try:
        channels = make_channels(settings)
    except AlertConfigError as error:
        print(f"Доставка алертов настроена неверно: {error}", file=sys.stderr)
        return FAILED

    if test:
        return _test(channels, settings)

    # Схему команда не правит: состояние отправленных алертов лежит в базе,
    # и работать на устаревшей схеме хуже, чем не запуститься.
    assert_current(engine)

    hours = settings.alert_repeat_hours if repeat_hours is None else repeat_hours
    with SessionLocal() as session:
        report = run_alerts(
            session,
            channels,
            repeat_hours=hours,
            prefix=settings.alert_subject_prefix,
            dry_run=dry_run,
        )

    code = _say(report, dry_run)
    if not dry_run:
        # Пульс — только после удачной проверки. Если проверка не состоялась
        # или алерт не ушёл, сигнала нет, и о беде узнает сторож снаружи
        # по его пропаже: это и есть ответ на «о своей смерти не сообщить».
        _pulse(settings, healthy=code == OK)
    return code


def _say(report: Report, dry_run: bool) -> int:
    """Печатает исход прогона и возвращает код возврата."""
    _print_channels(report)
    if not report.decisions:
        if report.open_problems:
            print(
                f"Новых поводов нет, но открытых сбоев: {report.open_problems}. "
                "О них уже сообщено — напоминание уйдёт по ALERT_REPEAT_HOURS."
            )
        else:
            print("Сообщать нечего: все активные кабинеты загружаются.")
        return OK

    for decision in report.decisions:
        mark = "восстановление" if decision.kind == RECOVERED else "сбой"
        print(f"  [{mark}] {decision.condition.subject} ({decision.kind})")

    if dry_run:
        print("Ничего не отправлено: прогон вхолостую.")
        return OK

    print(f"Отправлено поводов: {len(report.sent)}.")
    for line in report.failures:
        print(f"Канал не принял — {line}", file=sys.stderr)
    if report.undelivered:
        print(
            f"Не доставлено поводов: {len(report.undelivered)}. "
            "Отметка об отправке не поставлена — следующий прогон попробует снова.",
            file=sys.stderr,
        )
        return FAILED
    return OK


def _pulse(settings, healthy: bool = True) -> bool:
    """Дёргает адрес пульса, если он настроен (задача G-18).

    Возвращает ``False`` только в одном случае: пульс настроен и не ушёл.
    В обычном прогоне это не меняет кода возврата — загрузка данных и алерты
    от пульса не зависят, а о пропаже сигнала и так узнает тот, кто его ждёт;
    в проверке каналов (``--test``) считается отказом, потому что проверяется
    как раз исправность связи. Адрес наружу выходит маской: в нём ключ.
    """
    url = (getattr(settings, "heartbeat_url", "") or "").strip()
    if not url:
        return True
    if not healthy:
        # Молчание — и есть сигнал: сторож снаружи заметит пропажу сам.
        print(
            f"Пульс не отправлен: прогон неудачный. Сторож ({mask_url(url)}) "
            "поднимет тревогу, когда сигнал пропадёт."
        )
        return True
    try:
        send_heartbeat(url)
    except DeliveryError as error:
        event("пульс не ушёл", level=logging.WARNING, where=mask_url(url), reason=str(error))
        print(f"Пульс не ушёл на {mask_url(url)}: {error}", file=sys.stderr)
        return False
    event("пульс отправлен", where=mask_url(url))
    print(f"Пульс отправлен: {mask_url(url)}")
    return True


def _test(channels: list, settings) -> int:
    """Проверка связи целиком: каналы доставки и пульс наружу."""
    pulse_url = (getattr(settings, "heartbeat_url", "") or "").strip()
    if not channels and not pulse_url:
        print(
            "Проверять нечего: ни доставка алертов не настроена "
            "(SMTP_HOST, ALERT_WEBHOOK_URL), ни пульс наружу (HEARTBEAT_URL).",
            file=sys.stderr,
        )
        return FAILED

    prefix = settings.alert_subject_prefix
    moment = datetime.now(UTC).replace(microsecond=0)
    note = Note(
        subject=f"{prefix} проверка доставки".strip(),
        body=(
            "Это проверка канала доставки алертов, а не сбой.\n"
            f"Отправлено с машины {platform.node()} в {moment:%d.%m.%Y %H:%M} UTC.\n"
            "Если письмо или сообщение пришло — о сбоях загрузки сообщат так же."
        ),
    )
    if channels:
        print("Каналы доставки:")
        for channel in channels:
            print(f"  {channel.describe()}")

    sent, failures = deliver(channels, note) if channels else ([], [])
    for line in failures:
        print(f"Канал не принял — {line}", file=sys.stderr)
    if sent:
        print(f"Приняли: {', '.join(sent)}.")
    # Пульс проверяется вместе с каналами: сторож, которого ни разу
    # не дёргали, — такое же предположение, как непроверенный канал.
    pulsed = _pulse(settings)
    event(
        "проверка доставки алертов",
        level=logging.WARNING if failures or not pulsed else logging.INFO,
        sent=",".join(sent) or "-",
        failed=len(failures),
        pulse="ok" if pulsed else "failed",
    )
    return FAILED if failures or not pulsed else OK


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Сообщить о сбоях загрузки в почту и мессенджер",
        epilog="Коды возврата: 0 — сообщать нечего или отправлено, 1 — не доставлено.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, о чём сообщили бы, и ничего не отправлять",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="отправить проверочное сообщение во все настроенные каналы",
    )
    parser.add_argument(
        "--repeat-hours",
        type=int,
        default=None,
        help="через сколько часов напоминать о неисправленном сбое (0 — не напоминать)",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(main(dry_run=args.dry_run, test=args.test, repeat_hours=args.repeat_hours))
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(FAILED) from None
