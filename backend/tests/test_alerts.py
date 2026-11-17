"""Доставка алертов: правила, каналы и секреты в сообщениях (задача G-16).

Правила проверяются без сети и без почтового сервера — они чистые функции.
Каналы проверяются подставными: почта — подменённым ``smtplib.SMTP``, вебхук —
подменённым HTTP-клиентом. Живая отправка проверялась руками, в документации
это написано; в тестах настоящий SMTP не поднимается — тесты не должны
зависеть от сети.

Самое важное здесь — три свойства, при потере которых доставка алертов
становится хуже своего отсутствия:

* об одном и том же не сообщают каждый прогон (иначе алерты перестают читать);
* отметка «сообщили» ставится только после удачной отправки (иначе алерт
  теряется навсегда при минутной недоступности сервера);
* в сообщение и в лог не попадают ни ключи кабинетов, ни адрес вебхука.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select

from app.config import Settings
from app.db import SessionLocal
from app.models import AlertState, Store, SyncRun, SyncStatus
from app.services import notify
from app.services.alerts import (
    CHANGED,
    NEW,
    RECOVERED,
    REPEAT,
    Seen,
    conditions,
    decide,
    deliver,
    run_alerts,
)
from app.services.monitoring import FAILED, NEVER, OK, STALE, StoreSync, SyncHealth
from app.services.notify import (
    AlertConfigError,
    DeliveryError,
    EmailChannel,
    Note,
    WebhookChannel,
    check_config,
    make_channels,
    mask_url,
    redact,
    smtp_port,
)

NOW = datetime(2026, 11, 17, 9, 0)

#: Ключ кабинета WB — это JWT, и в тексте ошибки он узнаваем по виду.
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJpZCI6MX0.c2lnbmF0dXJl"

#: Адрес входящего вебхука: путь в нём и есть ключ.
HOOK = "https://chat.example.com/hooks/T0000/B1111/sEcReTkEy"


# ------------------------------------------------------------------ подставные каналы


class FakeChannel:
    name = "подставной"

    def __init__(self) -> None:
        self.notes: list[Note] = []

    def describe(self) -> str:
        return "подставной канал"

    def send(self, note: Note) -> None:
        self.notes.append(note)


class BrokenChannel:
    name = "сломанный"

    def describe(self) -> str:
        return "сломанный канал"

    def send(self, note: Note) -> None:
        raise DeliveryError("сервер не ответил")


def health(*rows: StoreSync, hours: int = 36) -> SyncHealth:
    return SyncHealth(stale_after_hours=hours, stores=list(rows))


def row(
    status: str,
    code: str = "store01",
    finished: datetime | None = None,
    message: str | None = None,
) -> StoreSync:
    return StoreSync(
        store_id=1,
        store_code=code,
        store_name="Основной кабинет",
        status=status,
        finished_at=finished,
        message=message,
    )


# ------------------------------------------------------------------------- поводы


def test_a_broken_cabinet_is_a_reason_to_write():
    found = conditions(health(row(FAILED, finished=NOW - timedelta(hours=1))), NOW)

    assert [c.key for c in found] == ["sync:store01"]
    assert found[0].state == FAILED
    assert "загрузка упала" in found[0].subject


def test_a_cabinet_that_never_loaded_is_not_a_reason():
    """Как и в готовности: кабинеты заводят раньше первой загрузки."""
    assert conditions(health(row(NEVER, code="new")), NOW) == []


def test_a_well_cabinet_is_carried_along_for_the_recovery_notice():
    found = conditions(health(row(OK, finished=NOW)), NOW)

    assert [c.state for c in found] == [OK]
    assert not found[0].is_problem


def test_the_message_names_the_cabinet_the_time_and_where_to_look():
    found = conditions(
        health(row(FAILED, finished=NOW - timedelta(hours=2), message="HTTPError: 401")), NOW
    )
    body = found[0].body

    assert "Основной кабинет (store01)" in body
    assert "17.11.2026 07:00 UTC" in body
    assert "HTTPError: 401" in body
    assert "Куда смотреть" in body


def test_a_lagging_cabinet_says_how_long_it_lags():
    found = conditions(health(row(STALE, finished=NOW - timedelta(hours=50)), hours=36), NOW)

    assert "уже 50 ч при пороге 36 ч" in found[0].body


# ----------------------------------------------------------------------- решения


def test_a_new_problem_is_reported():
    found = conditions(health(row(FAILED, finished=NOW)), NOW)

    assert [d.kind for d in decide(found, {}, NOW)] == [NEW]


def test_the_same_problem_is_not_reported_twice():
    """Алерт каждый прогон перестают читать — и доставка теряет смысл."""
    found = conditions(health(row(FAILED, finished=NOW)), NOW)
    known = {"sync:store01": Seen(FAILED, NOW - timedelta(hours=1), NOW - timedelta(hours=1))}

    assert decide(found, known, NOW, repeat_hours=24) == []


def test_an_unfixed_problem_is_recalled_after_the_window():
    found = conditions(health(row(FAILED, finished=NOW)), NOW)
    known = {"sync:store01": Seen(FAILED, NOW - timedelta(hours=30), NOW - timedelta(days=2))}

    decisions = decide(found, known, NOW, repeat_hours=24)

    assert [d.kind for d in decisions] == [REPEAT]
    assert "не меняется с 15.11.2026 09:00 UTC" in decisions[0].note.body


def test_recalls_can_be_switched_off():
    found = conditions(health(row(FAILED, finished=NOW)), NOW)
    known = {"sync:store01": Seen(FAILED, NOW - timedelta(days=30), NOW - timedelta(days=30))}

    assert decide(found, known, NOW, repeat_hours=0) == []


def test_a_changed_state_is_reported_at_once():
    """«Упала» и «отстала» — разные новости, повтором это не считается."""
    found = conditions(health(row(STALE, finished=NOW - timedelta(hours=50))), NOW)
    known = {"sync:store01": Seen(FAILED, NOW - timedelta(minutes=5), NOW - timedelta(minutes=5))}

    assert [d.kind for d in decide(found, known, NOW, repeat_hours=24)] == [CHANGED]


def test_recovery_is_reported_once():
    found = conditions(health(row(OK, finished=NOW)), NOW)
    known = {"sync:store01": Seen(FAILED, NOW - timedelta(hours=1), NOW - timedelta(hours=3))}

    assert [d.kind for d in decide(found, known, NOW)] == [RECOVERED]
    assert decide(found, {}, NOW) == [], "о благополучии без прошлого сбоя писать не о чем"


def test_silence_when_everything_is_well():
    found = conditions(health(row(OK, finished=NOW), row(OK, code="store02", finished=NOW)), NOW)

    assert decide(found, {}, NOW) == []


# -------------------------------------------------------------------- секреты


def test_a_key_in_the_message_is_masked():
    """Текст ошибки приезжает из чужой библиотеки, а алерт уходит наружу."""
    found = conditions(health(row(FAILED, finished=NOW, message=f"401 token={TOKEN}")), NOW)
    body = decide(found, {}, NOW)[0].note.body

    assert TOKEN not in body
    assert "eyJ" not in body
    assert "***" in body


def test_redaction_keeps_the_rest_of_the_text():
    assert redact("Ошибка 401 у store01") == "Ошибка 401 у store01"
    assert redact(f"Authorization: {TOKEN}") == "Authorization: ***"


def test_the_webhook_address_never_leaves_the_process():
    channel = WebhookChannel(url=HOOK)

    assert "sEcReTkEy" not in channel.describe()
    assert mask_url(HOOK) == "https://chat.example.com/***"
    assert mask_url("https://user:pass@chat.example.com/hooks/x") == "https://chat.example.com/***"
    assert mask_url("не адрес") == "***"


def test_a_webhook_failure_does_not_print_the_address(monkeypatch):
    """В тексте ошибки HTTP-клиента стоит полный адрес, то есть ключ."""
    request = httpx.Request("POST", HOOK)
    response = httpx.Response(403, request=request)

    def fail(*args, **kwargs):
        raise httpx.HTTPStatusError("403 for url " + HOOK, request=request, response=response)

    monkeypatch.setattr(notify.httpx, "post", fail)
    channel = WebhookChannel(url=HOOK)

    with pytest.raises(DeliveryError) as error:
        channel.send(Note("тема", "текст"))

    assert "sEcReTkEy" not in str(error.value)
    assert "403" in str(error.value)


def test_the_http_client_does_not_log_the_webhook_address(caplog):
    """Чужой клиент печатает строку запроса целиком — вместе с ключом вебхука.

    Это нашлось живой проверкой: адрес, аккуратно замаскированный в своём
    выводе, httpx тут же печатал в лог полностью.
    """
    from app.observability import configure_logging

    configure_logging()
    logging.getLogger("httpx").info("HTTP Request: POST %s", HOOK)

    assert HOOK not in caplog.text
    assert logging.getLogger("httpx").getEffectiveLevel() > logging.INFO


def test_a_key_in_a_channel_failure_is_masked():
    """Текст отказа канала уходит в лог и в вывод команды."""

    class Leaky:
        name = "болтливый"

        def describe(self) -> str:
            return "болтливый канал"

        def send(self, note: Note) -> None:
            raise DeliveryError(f"отказ сервера: token={TOKEN}")

    sent, failures = deliver([Leaky()], Note("тема", "текст"))

    assert sent == []
    assert TOKEN not in failures[0]


# ------------------------------------------------------------------- настройки


def test_without_settings_there_are_no_channels():
    """По умолчанию система ведёт себя в точности как до задачи."""
    assert make_channels(Settings()) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"smtp_host": "smtp.example.com"},
        {"alert_email_to": "ops@example.com"},
        {"smtp_host": "smtp.example.com", "alert_email_to": "ops@example.com"},
        {
            "smtp_host": "smtp.example.com",
            "alert_email_to": "ops@example.com",
            "alert_email_from": "raketa@example.com",
            "smtp_security": "tls",
        },
        {"alert_webhook_url": "chat.example.com/hooks/x"},
        {"alert_webhook_chat_id": "-100500"},
        {"alert_repeat_hours": -1},
    ],
)
def test_half_configured_delivery_refuses_to_start(overrides):
    """«Алерты настроены» и «алерты не уходят» вместе — худший из исходов."""
    with pytest.raises(AlertConfigError):
        check_config(Settings(**overrides))


def test_a_fully_configured_pair_of_channels_is_accepted():
    settings = Settings(
        smtp_host="smtp.example.com",
        alert_email_to="ops@example.com, boss@example.com",
        alert_email_from="raketa@example.com",
        alert_webhook_url=HOOK,
        alert_webhook_chat_id="-100500",
    )

    channels = make_channels(settings)

    assert [type(channel) for channel in channels] == [EmailChannel, WebhookChannel]
    assert channels[0].recipients == ("ops@example.com", "boss@example.com")
    assert channels[1].chat_id == "-100500"


def test_the_port_follows_the_mode():
    assert smtp_port(Settings(smtp_security="ssl")) == 465
    assert smtp_port(Settings(smtp_security="starttls")) == 587
    assert smtp_port(Settings(smtp_security="none")) == 25
    assert smtp_port(Settings(smtp_security="ssl", smtp_port=2525)) == 2525


# ---------------------------------------------------------------------- почта


class FakeSMTP:
    """Подставной почтовый сервер: запоминает, что с ним делали."""

    opened: list[tuple[str, int]] = []
    messages: list[object] = []
    calls: list[str] = []

    def __init__(self, host, port, timeout=None):
        FakeSMTP.opened.append((host, port))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        FakeSMTP.calls.append("starttls")

    def login(self, user, password):
        FakeSMTP.calls.append(f"login:{user}")

    def send_message(self, message):
        FakeSMTP.messages.append(message)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.opened, FakeSMTP.messages, FakeSMTP.calls = [], [], []
    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def test_the_letter_carries_the_subject_the_text_and_the_recipients(fake_smtp):
    channel = EmailChannel(
        host="smtp.example.com",
        port=587,
        sender="raketa@example.com",
        recipients=("ops@example.com", "boss@example.com"),
        user="raketa",
        password="секрет",
    )

    channel.send(Note("[Ракета] store01 — загрузка упала", "Кабинет: Основной"))

    letter = fake_smtp.messages[0]
    assert letter["Subject"] == "[Ракета] store01 — загрузка упала"
    assert letter["To"] == "ops@example.com, boss@example.com"
    assert "Кабинет: Основной" in letter.get_content()
    assert fake_smtp.calls == ["starttls", "login:raketa"], "шифрование и вход — в этом порядке"
    assert "секрет" not in str(fake_smtp.calls)


def test_a_mail_server_failure_becomes_a_delivery_error(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("соединение отклонено")

    monkeypatch.setattr(notify.smtplib, "SMTP", fail)
    channel = EmailChannel(host="smtp.example.com", port=587, sender="a@b", recipients=("c@d",))

    with pytest.raises(DeliveryError):
        channel.send(Note("тема", "текст"))


# ----------------------------------------------------------- отправка и память


@pytest.fixture
def broken_store(client):
    """Кабинет с упавшим прогоном — и уборка за собой."""
    created = client.post(
        "/api/v1/catalog/stores", json={"code": "g16", "name": "Кабинет для алертов"}
    )
    assert created.status_code == 201, created.text
    store_id = created.json()["id"]
    with SessionLocal() as session:
        run = SyncRun(
            store_id=store_id,
            status=SyncStatus.FAILED,
            message="RuntimeError: проверка G-16",
            finished_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    yield store_id
    with SessionLocal() as session:
        session.execute(delete(AlertState))
        session.delete(session.get(SyncRun, run_id))
        session.delete(session.get(Store, store_id))
        session.commit()


def _keys() -> list[str]:
    with SessionLocal() as session:
        return list(session.scalars(select(AlertState.key)))


def _run(channels, **kwargs):
    with SessionLocal() as session:
        return run_alerts(session, channels, prefix="[Ракета]", **kwargs)


def test_a_failed_load_is_reported_and_remembered(broken_store):
    channel = FakeChannel()

    report = _run([channel])

    assert report.sent == ["sync:g16"]
    assert "sync:g16" in _keys()
    assert channel.notes[0].subject.startswith("[Ракета] g16 —")
    assert "Кабинет для алертов" in channel.notes[0].body


def test_the_second_run_stays_silent(broken_store):
    channel = FakeChannel()

    _run([channel])
    second = _run([channel])

    assert second.decisions == []
    assert len(channel.notes) == 1
    assert second.open_problems == 1, "«сообщать нечего» при открытом сбое было бы неправдой"


def test_the_mark_is_set_only_after_a_successful_send(broken_store):
    """Иначе минутная недоступность сервера означала бы потерянный алерт."""
    first = _run([BrokenChannel()])

    assert first.undelivered == ["sync:g16"]
    assert first.failures and "сломанный" in first.failures[0]
    assert _keys() == [], "о недоставленном алерте помнить нельзя"

    working = FakeChannel()
    second = _run([working])

    assert second.sent == ["sync:g16"]
    assert len(working.notes) == 1


def test_one_working_channel_is_enough(broken_store):
    """Иначе один сломанный канал превратил бы второй в поток повторов."""
    working = FakeChannel()

    report = _run([BrokenChannel(), working])

    assert report.sent == ["sync:g16"]
    assert report.failures, "о сломанном канале всё равно нужно сказать"
    assert len(working.notes) == 1


def test_no_channels_means_undelivered(broken_store):
    report = _run([])

    assert report.undelivered == ["sync:g16"]
    assert _keys() == []


def test_a_dry_run_sends_nothing_and_remembers_nothing(broken_store):
    channel = FakeChannel()

    report = _run([channel], dry_run=True)

    assert [d.kind for d in report.decisions] == [NEW]
    assert channel.notes == []
    assert _keys() == []


def test_recovery_is_reported_and_clears_the_memory(broken_store):
    channel = FakeChannel()
    _run([channel])
    assert _keys() == ["sync:g16"]

    # Кабинет загрузился: следующий завершённый прогон — удачный.
    with SessionLocal() as session:
        session.add(
            SyncRun(
                store_id=broken_store,
                status=SyncStatus.SUCCESS,
                facts_upserted=5,
                finished_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        session.commit()
    try:
        report = _run([channel])

        assert [d.kind for d in report.decisions] == [RECOVERED]
        assert "восстановилась" in channel.notes[-1].subject
        assert _keys() == [], "повод исчез — помнить о нём больше нечего"
    finally:
        with SessionLocal() as session:
            session.execute(
                delete(SyncRun).where(
                    SyncRun.store_id == broken_store, SyncRun.status == SyncStatus.SUCCESS
                )
            )
            session.commit()


# ------------------------------------------------------------------- команда


def test_the_command_says_there_is_nothing_to_report(client, capsys):
    from app import alerts as cli

    assert cli.main() == cli.OK
    assert "не настроена" in capsys.readouterr().out


def test_the_command_returns_one_when_nothing_was_delivered(broken_store, monkeypatch, capsys):
    from app import alerts as cli

    monkeypatch.setattr(cli, "make_channels", lambda settings: [BrokenChannel()])

    assert cli.main() == cli.FAILED
    assert _keys() == []
    assert "Не доставлено" in capsys.readouterr().err


def test_the_command_can_run_dry(broken_store, monkeypatch, capsys):
    from app import alerts as cli

    channel = FakeChannel()
    monkeypatch.setattr(cli, "make_channels", lambda settings: [channel])

    assert cli.main(dry_run=True) == cli.OK
    assert channel.notes == []
    assert "вхолостую" in capsys.readouterr().out


def test_the_test_flag_proves_the_channel_without_touching_state(broken_store, monkeypatch, capsys):
    """Канал, в который ни разу не отправляли, — предположение о канале."""
    from app import alerts as cli

    channel = FakeChannel()
    monkeypatch.setattr(cli, "make_channels", lambda settings: [channel])

    assert cli.main(test=True) == cli.OK
    assert "проверка" in channel.notes[0].subject
    assert "не сбой" in channel.notes[0].body
    assert _keys() == [], "проверка каналов не должна считаться отправленным алертом"
    assert "Приняли" in capsys.readouterr().out


def test_the_test_flag_fails_without_channels(client):
    from app import alerts as cli

    assert cli.main(test=True) == cli.FAILED


def test_broken_settings_stop_the_command(monkeypatch, capsys):
    from app import alerts as cli

    def refuse(settings):
        raise AlertConfigError("SMTP_HOST задан, а ALERT_EMAIL_TO пуст")

    monkeypatch.setattr(cli, "make_channels", refuse)

    assert cli.main() == cli.FAILED
    assert "настроена неверно" in capsys.readouterr().err


def test_delivery_failure_does_not_break_the_load(broken_store, monkeypatch):
    """Планировщику важно, загрузились ли данные, а не дошло ли письмо."""
    from app import sync as sync_cli

    def explode(settings):
        raise RuntimeError("почтовый сервер недоступен")

    monkeypatch.setattr("app.services.notify.make_channels", explode)

    sync_cli.notify(Settings())  # не должно поднять ошибку
