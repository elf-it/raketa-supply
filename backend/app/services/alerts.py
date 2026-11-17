"""О чём сообщать дежурному и когда молчать (задача G-16).

Состояние загрузки система умела показывать: готовностью наружу и плашкой
человеку (задача G-4). Но сама о себе она не сообщала — сбой замечал тот, кто
догадался посмотреть. Здесь появляется третья дорога: система говорит о сбое
сама, в почту или мессенджер.

Решения и почему так:

* **Правила отделены от доставки.** Этот модуль решает, что считать поводом;
  чем отправить — дело ``services/notify.py``. Поэтому вся логика «сообщать
  или молчать» проверяется без сети и без почтового сервера.

* **Повторы ограничены.** Алерт, приходящий каждый прогон, перестают читать —
  и доставка становится хуже своего отсутствия: она создаёт уверенность, что
  о сбое сообщат. Поэтому о неисправленном сбое напоминается не чаще
  ``ALERT_REPEAT_HOURS``, а смена состояния (упала → отстала) сообщается сразу:
  это другая новость.

* **О восстановлении сообщается тоже.** Иначе дежурный, получивший «упала»,
  не узнает, что всё прошло, и пойдёт разбираться с уже исправленным.

* **Отметка «сообщили» ставится только после удачной отправки.** Иначе
  недоступный на минуту почтовый сервер означал бы потерянный навсегда алерт:
  система считала бы, что уже сообщила.

* **Доставлено — значит принял хотя бы один канал.** Если почта ушла,
  а вебхук упал, повод считается сообщённым: дежурный узнал. Иначе один
  сломанный канал превратил бы второй в поток повторов.

* **Чего эта задача не делает.** Сообщать о себе система может, только пока
  работает её процесс: алерты отправляет та же команда, что загружает данные,
  или отдельный прогон по расписанию. Если машина выключена, планировщик
  мёртв или база недоступна — отправить некому и нечем, состояние доставленных
  алертов живёт в той же базе. Это не недоделка, а предел любого алерта изнутри:
  о смерти самого себя сообщить нельзя. Поэтому внешний мониторинг, опрашивающий
  ``/health/ready``, остаётся нужен — он снаружи.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AlertState
from ..observability import event
from .monitoring import FAILED, NEVER, OK, STALE, StoreSync, SyncHealth, sync_health
from .notify import Channel, DeliveryError, Note, redact

#: Виды решений. ``new`` — повод появился, ``changed`` — состояние сменилось
#: на другое плохое, ``repeat`` — напоминание, ``recovered`` — всё прошло.
NEW = "new"
CHANGED = "changed"
REPEAT = "repeat"
RECOVERED = "recovered"

#: Человеческие названия состояний для темы письма.
TITLES = {
    FAILED: "загрузка упала",
    STALE: "данные отстали",
    OK: "загрузка восстановилась",
}

KIND_NOTES = {
    NEW: "",
    CHANGED: "Состояние изменилось.",
    REPEAT: "Сообщается повторно: состояние не меняется.",
    RECOVERED: "",
}


def _now() -> datetime:
    """UTC без зоны — как хранит база."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class Condition:
    """Повод в его нынешнем состоянии: ключ, состояние и готовый текст."""

    key: str
    state: str
    subject: str
    body: str

    @property
    def is_problem(self) -> bool:
        return self.state != OK


@dataclass(frozen=True)
class Seen:
    """Что об этом поводе сообщали в прошлый раз."""

    state: str
    last_sent_at: datetime
    since: datetime


@dataclass(frozen=True)
class Decision:
    condition: Condition
    kind: str
    since: datetime | None = None

    @property
    def note(self) -> Note:
        """Готовое сообщение. Приставка темы добавляется при отправке."""
        lines = [self.condition.body]
        tail = KIND_NOTES.get(self.kind, "")
        if self.kind == REPEAT and self.since is not None:
            tail = f"Сообщается повторно: состояние не меняется с {_stamp(self.since)}."
        if tail:
            lines.append(tail)
        return Note(subject=self.condition.subject, body=redact("\n\n".join(lines)))


def _stamp(moment: datetime | None) -> str:
    return f"{moment:%d.%m.%Y %H:%M} UTC" if moment else "неизвестно"


def _sync_body(row: StoreSync, health: SyncHealth, now: datetime) -> str:
    """Текст про один кабинет: что случилось, когда и куда смотреть.

    Названия кабинетов здесь есть — в отличие от ответа готовности, которая
    открыта без входа. Алерт уходит известному получателю, и без названия
    кабинета он бесполезен: «что-то отстало» не говорит, с чего начинать.
    """
    lines = [
        f"Кабинет: {row.store_name} ({row.store_code})",
        f"Состояние: {TITLES.get(row.status, row.status)}",
        f"Последний завершённый прогон: {_stamp(row.finished_at)}",
    ]
    if row.status == STALE and row.finished_at is not None:
        hours = (now - row.finished_at).total_seconds() / 3600
        lines.append(f"Данных нет уже {hours:.0f} ч при пороге {health.stale_after_hours} ч.")
    if row.message:
        lines.append(f"Из журнала прогона: {row.message[:500]}")
    if row.status == FAILED:
        lines.append(
            "Куда смотреть: журнал загрузок на экране «Загрузка», "
            "лог процесса загрузки, доступность кабинета WB."
        )
    elif row.status == STALE:
        lines.append(
            "Куда смотреть: работает ли планировщик, которым запускается "
            "`python -m app.sync`, и проходят ли его прогоны."
        )
    return "\n".join(lines)


def conditions(health: SyncHealth, now: datetime | None = None) -> list[Condition]:
    """Поводы по состоянию загрузки — включая благополучные.

    Благополучные нужны затем, что по ним решается сообщение о восстановлении:
    без них дежурный остался бы с открытым «упала» навсегда.

    Кабинет, который ни разу не загружался (``never``), поводом не считается —
    как и в готовности: на свежем развёртывании кабинеты заводят раньше первой
    загрузки, и алерт об этом разбудил бы дежурного в день установки.
    """
    moment = now or _now()
    out: list[Condition] = []
    for row in health.stores:
        if row.status == NEVER:
            continue
        title = TITLES.get(row.status, row.status)
        out.append(
            Condition(
                key=f"sync:{row.store_code}",
                state=row.status,
                subject=f"{row.store_code} — {title}",
                body=_sync_body(row, health, moment),
            )
        )
    return out


def decide(
    found: list[Condition],
    known: dict[str, Seen],
    now: datetime | None = None,
    repeat_hours: int = 24,
) -> list[Decision]:
    """Что из найденного стоит отправить. Чистая функция — без базы и сети."""
    moment = now or _now()
    out: list[Decision] = []
    for condition in found:
        seen = known.get(condition.key)
        if condition.is_problem:
            if seen is None:
                out.append(Decision(condition, NEW))
            elif seen.state != condition.state:
                out.append(Decision(condition, CHANGED))
            elif repeat_hours > 0 and moment - seen.last_sent_at >= timedelta(hours=repeat_hours):
                out.append(Decision(condition, REPEAT, since=seen.since))
            continue
        # Состояние благополучное: сообщать не о чем, кроме случая, когда
        # в прошлый раз сообщили о сбое.
        if seen is not None:
            out.append(Decision(condition, RECOVERED, since=seen.since))
    return out


def load_seen(session: Session) -> dict[str, Seen]:
    rows = session.scalars(select(AlertState)).all()
    return {
        row.key: Seen(state=row.state, last_sent_at=row.last_sent_at, since=row.since)
        for row in rows
    }


def deliver(channels: list[Channel], note: Note) -> tuple[list[str], list[str]]:
    """Отправляет во все каналы. Возвращает (кто принял, что не вышло)."""
    sent: list[str] = []
    failures: list[str] = []
    for channel in channels:
        try:
            channel.send(note)
        except DeliveryError as error:
            failures.append(f"{channel.name}: {redact(str(error))}")
        except Exception as error:  # noqa: BLE001 — канал не должен валить прогон
            failures.append(f"{channel.name}: {type(error).__name__}: {redact(str(error))}")
        else:
            sent.append(channel.name)
    return sent, failures


@dataclass
class Report:
    """Что команда сделала — для вывода планировщику и для лога."""

    channels: list[str] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    #: Сколько поводов открыто прямо сейчас — в том числе тех, о которых уже
    #: сообщили. Без этого числа вывод «сообщать нечего» читался бы как
    #: «всё хорошо», а значит он был бы неправдой при открытом сбое.
    open_problems: int = 0
    sent: list[str] = field(default_factory=list)
    undelivered: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.undelivered


def _remember(session: Session, decision: Decision, now: datetime) -> None:
    """Отмечает, что об этом сообщили. Зовётся только после удачной отправки."""
    row = session.scalars(
        select(AlertState).where(AlertState.key == decision.condition.key)
    ).first()
    if decision.kind == RECOVERED:
        # Повод исчез — память о нём больше ни на что не влияет.
        if row is not None:
            session.delete(row)
        session.commit()
        return
    if row is None:
        session.add(
            AlertState(
                key=decision.condition.key,
                state=decision.condition.state,
                since=now,
                last_sent_at=now,
                sends=1,
            )
        )
    else:
        if row.state != decision.condition.state:
            row.since = now
        row.state = decision.condition.state
        row.last_sent_at = now
        row.sends += 1
    session.commit()


def run_alerts(
    session: Session,
    channels: list[Channel],
    now: datetime | None = None,
    repeat_hours: int = 24,
    prefix: str = "",
    dry_run: bool = False,
) -> Report:
    """Проверяет состояние, отправляет что нужно, запоминает отправленное."""
    moment = now or _now()
    report = Report(channels=[channel.describe() for channel in channels])

    health = sync_health(session, now=moment)
    found = conditions(health, moment)
    report.open_problems = sum(1 for condition in found if condition.is_problem)
    report.decisions = decide(found, load_seen(session), moment, repeat_hours)
    if not report.decisions or dry_run:
        return report

    for decision in report.decisions:
        note = decision.note
        if prefix:
            note = Note(subject=f"{prefix} {note.subject}", body=note.body)
        sent, failures = deliver(channels, note)
        report.failures.extend(failures)
        if sent:
            report.sent.append(decision.condition.key)
            _remember(session, decision, moment)
            event(
                "алерт отправлен",
                key=decision.condition.key,
                kind=decision.kind,
                channels=",".join(sent),
            )
        else:
            # Отметка не ставится: иначе недоступный на минуту сервер означал бы
            # навсегда потерянный алерт.
            report.undelivered.append(decision.condition.key)
            event(
                "алерт не доставлен",
                level=logging.ERROR,
                key=decision.condition.key,
                kind=decision.kind,
                reason="; ".join(failures) or "каналы доставки не настроены",
            )
    return report
