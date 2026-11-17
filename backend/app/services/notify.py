"""Чем доставлять алерты: почта и вебхук мессенджера (задача G-16).

Этот модуль знает только «как отправить сообщение», и ничего — о том, что
считать поводом: поводы живут в ``services/alerts.py``. Разделение не
формальное: правила алертов проверяются тестами без сети и без почтового
сервера, а каналы — отдельно, подставными серверами.

Решения и почему так:

* **Два канала, и оба необязательные.** Почта — потому что она есть у всех
  и к ней привыкли дежурные. Вебхук — потому что сбой ночью читают в
  мессенджере, а не в почте. Куда именно слать, зависит от того, где система
  живёт и кто её сопровождает (вопрос Q-16), поэтому по умолчанию не настроен
  ни один: без настроек система ведёт себя в точности как до этой задачи.

* **Полунастроенный канал — отказ старта.** Заполненный ``SMTP_HOST`` без
  получателей выглядит как настроенные алерты, а ведёт себя как их отсутствие.
  Это худший из возможных исходов: о сбое никто не узнает, но все уверены,
  что узнают. Проверка зовётся из ``Settings.validate_runtime``.

* **Адрес вебхука — секрет.** В нём ключ входящего вебхука (у Slack и
  Mattermost — прямо в пути), поэтому наружу он выходит только маской:
  ни в выводе команды, ни в логе, ни в тексте ошибки доставки его нет.
  Ошибка от HTTP-клиента содержит полный адрес, и отдавать её как есть
  значило бы печатать ключ в лог при каждом сбое доставки.

* **Из сообщений вычищается похожее на ключ.** Текст ошибки загрузки
  приезжает из журнала прогонов, то есть из чужой библиотеки; ключи кабинетов
  WB — это JWT, и узнать их по виду легко. Алерт уходит наружу, в чужую почту
  и чужой мессенджер, и дешевле замаскировать лишнее, чем однажды отправить
  ключ в групповой чат.
"""

from __future__ import annotations

import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:  # pragma: no cover — только для подсказок типов
    from ..config import Settings

#: Сколько ждать почтовый сервер и вебхук. Доставка идёт в том же процессе,
#: что загрузка данных, и висеть на недоступном сервере она не должна.
TIMEOUT_SECONDS = 10.0

SECURITY_NONE = "none"
SECURITY_STARTTLS = "starttls"
SECURITY_SSL = "ssl"
SECURITIES = (SECURITY_NONE, SECURITY_STARTTLS, SECURITY_SSL)

#: Порт по режиму, если он не задан явно.
DEFAULT_PORTS = {SECURITY_SSL: 465, SECURITY_STARTTLS: 587, SECURITY_NONE: 25}

#: Похожее на ключ: JWT (ключи кабинетов WB) и значение заголовка авторизации.
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{8,}[A-Za-z0-9_\-.]*")
_BEARER = re.compile(r"(?i)\b(bearer|authorization|token|api[-_]?key)\s*[:=]\s*\S+")

MASK = "***"


class AlertConfigError(RuntimeError):
    """Настройки доставки заполнены так, что алерт не уйдёт."""


class DeliveryError(RuntimeError):
    """Канал не принял сообщение. Текст пригоден для лога: без секретов."""


def redact(text: str) -> str:
    """Убирает из текста похожее на ключ. Для всего, что уходит наружу."""
    cleaned = _BEARER.sub(lambda m: f"{m.group(1)}: {MASK}", text or "")
    return _JWT.sub(MASK, cleaned)


def mask_url(url: str) -> str:
    """Адрес без пути и строки запроса: у входящего вебхука они и есть ключ."""
    parts = urlsplit(url or "")
    if not parts.netloc:
        return MASK
    host = parts.netloc.split("@")[-1]  # пароль в адресе наружу не выходит
    tail = f"/{MASK}" if parts.path.strip("/") or parts.query else ""
    return f"{parts.scheme}://{host}{tail}"


@dataclass(frozen=True)
class Note:
    """Сообщение: тема и текст. Одно и то же уходит во все каналы."""

    subject: str
    body: str

    def as_text(self) -> str:
        return f"{self.subject}\n\n{self.body}"


class Channel(Protocol):
    """Канал доставки. Отправка либо прошла, либо подняла ``DeliveryError``."""

    name: str

    def describe(self) -> str: ...

    def send(self, note: Note) -> None: ...


@dataclass
class EmailChannel:
    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    user: str = ""
    password: str = ""
    security: str = SECURITY_STARTTLS
    timeout: float = TIMEOUT_SECONDS

    name = "почта"

    def describe(self) -> str:
        return (
            f"почта → {', '.join(self.recipients)} "
            f"через {self.host}:{self.port} ({self.security})"
        )

    def _open(self) -> smtplib.SMTP:
        if self.security == SECURITY_SSL:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def send(self, note: Note) -> None:
        message = EmailMessage()
        message["Subject"] = note.subject
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message["Date"] = formatdate(localtime=True)
        message.set_content(note.body)
        try:
            with self._open() as server:
                if self.security == SECURITY_STARTTLS:
                    server.starttls()
                if self.user:
                    server.login(self.user, self.password)
                server.send_message(message)
        except (OSError, smtplib.SMTPException) as error:
            raise DeliveryError(redact(f"{type(error).__name__}: {error}")) from None


@dataclass
class WebhookChannel:
    url: str
    chat_id: str = ""
    timeout: float = TIMEOUT_SECONDS

    name = "вебхук"

    def describe(self) -> str:
        tail = f", чат {self.chat_id}" if self.chat_id else ""
        return f"вебхук → {mask_url(self.url)}{tail}"

    def send(self, note: Note) -> None:
        # ``text`` понимают входящие вебхуки Slack и Mattermost, ``chat_id``
        # с тем же ``text`` — Telegram. Отдельного режима под каждый
        # мессенджер здесь нет: это снова выбор за заказчика (вопрос Q-16).
        payload: dict[str, str] = {"text": note.as_text()}
        if self.chat_id:
            payload["chat_id"] = self.chat_id
        try:
            response = httpx.post(self.url, json=payload, timeout=self.timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            # Только код ответа: в тексте ошибки клиента стоит полный адрес,
            # то есть ключ вебхука.
            raise DeliveryError(f"ответ {error.response.status_code}") from None
        except httpx.HTTPError as error:
            raise DeliveryError(type(error).__name__) from None


def smtp_port(settings: Settings) -> int:
    if settings.smtp_port:
        return settings.smtp_port
    return DEFAULT_PORTS.get(settings.smtp_security.strip().lower(), 587)


def check_config(settings: Settings) -> None:
    """Отказ на настройках, при которых алерт не уйдёт или уйдёт не туда."""
    security = settings.smtp_security.strip().lower()
    host = settings.smtp_host.strip()
    recipients = settings.alert_email_list

    if host and not recipients:
        raise AlertConfigError(
            "SMTP_HOST задан, а ALERT_EMAIL_TO пуст: письма отправлять некому. "
            "Укажите получателей или уберите SMTP_HOST."
        )
    if recipients and not host:
        raise AlertConfigError("ALERT_EMAIL_TO задан, а SMTP_HOST пуст: письма отправлять нечем.")
    if host and not settings.alert_email_from.strip():
        raise AlertConfigError(
            "ALERT_EMAIL_FROM пуст: письмо без обратного адреса почтовые "
            "сервисы отбрасывают молча."
        )
    if host and security not in SECURITIES:
        raise AlertConfigError(
            f"SMTP_SECURITY={settings.smtp_security!r}: ожидается " f"{' | '.join(SECURITIES)}."
        )
    url = settings.alert_webhook_url.strip()
    if url and not url.startswith(("http://", "https://")):
        raise AlertConfigError("ALERT_WEBHOOK_URL должен начинаться с http:// или https://.")
    if settings.alert_webhook_chat_id.strip() and not url:
        raise AlertConfigError("ALERT_WEBHOOK_CHAT_ID задан без ALERT_WEBHOOK_URL: слать некуда.")
    if settings.alert_repeat_hours < 0:
        raise AlertConfigError("ALERT_REPEAT_HOURS не может быть отрицательным.")


def make_channels(settings: Settings) -> list[Channel]:
    """Каналы по настройкам. Пустой список — доставка не настроена."""
    check_config(settings)
    channels: list[Channel] = []
    if settings.smtp_host.strip():
        channels.append(
            EmailChannel(
                host=settings.smtp_host.strip(),
                port=smtp_port(settings),
                sender=settings.alert_email_from.strip(),
                recipients=tuple(settings.alert_email_list),
                user=settings.smtp_user,
                password=settings.smtp_password,
                security=settings.smtp_security.strip().lower(),
            )
        )
    if settings.alert_webhook_url.strip():
        channels.append(
            WebhookChannel(
                url=settings.alert_webhook_url.strip(),
                chat_id=settings.alert_webhook_chat_id.strip(),
            )
        )
    return channels
