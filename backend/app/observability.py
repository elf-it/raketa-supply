"""Логи и номер запроса (задача G-4).

До этой задачи в приложении не было ни одной строки логирования: всё, что
оставалось после сбоя, — журнал обращений uvicorn и трассировка в stderr
без всякого контекста. На вопрос «почему вчера в обед синхронизация упала
и кто в это время работал» ответить было нечем.

Решения и почему так:

* **Одна строка на запрос.** Метод, путь, код ответа, время и кто его сделал —
  этого хватает, чтобы найти медленное место и понять, чей именно запрос упал.
  Тело запроса не пишется: там правки прогноза и файлы по несколько мегабайт.

* **Номер запроса.** Он есть в каждой строке лога и уезжает в заголовке
  ``X-Request-Id``, поэтому сообщение об ошибке на экране можно связать
  с записью в логе. Номер из заголовка от прокси сохраняется — иначе один
  запрос получил бы в двух системах два разных номера.

  Номер — ещё и единственный надёжный способ узнать, кто сделал то, о чём
  пишет событие внутри обработчика (``кабинет загружен`` и подобные): такие
  строки выходят с ``user=-``. Обработчики синхронные, FastAPI выполняет их
  и их зависимости в отдельных потоках с копией контекста, и отметка о человеке,
  поставленная в проверке доступа, до них не доезжает. В строке самого запроса
  она есть — её пишет middleware, которому отметка приходит через состояние
  запроса, — а номер у обеих строк один.

* **Ни паролей, ни токенов, ни почты.** Лог читают шире, чем базу: он уезжает
  в файлы, в сборщики, в чужие глаза. Человек в логе — это его идентификатор
  и роль, а в неудачном входе почта маскируется до первой буквы: для разбора
  перебора этого достаточно, для утечки адресов — нет. Строка запроса тоже
  не пишется: в ней поисковые запросы и фильтры.

* **Текст или JSON.** По умолчанию человекочитаемый текст: его читают глазами
  в терминале. ``LOG_FORMAT=json`` даёт строку на событие для сборщика логов —
  какого именно, зависит от схемы развёртывания (вопрос Q-16).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

#: Имя логгера приложения. Всё своё пишется под ним, чужие библиотеки — под своими.
LOGGER_NAME = "raketa"

logger = logging.getLogger(LOGGER_NAME)

#: Заголовок номера запроса: его ставит прокси, а если не ставит — ставим мы.
REQUEST_ID_HEADER = "X-Request-Id"

#: Номер запроса годится, только если он похож на номер: иначе чужой заголовок
#: дотянулся бы до строк лога и до чужих систем.
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_actor: ContextVar[str] = ContextVar("actor", default="-")


def request_id() -> str:
    return _request_id.get()


def set_actor(user_id: int, role: str, request: Request | None = None) -> None:
    """Кто делает запрос. Зовётся из проверки доступа: раньше неё это неизвестно.

    Отметка кладётся в двух местах, и оба нужны. Контекстная переменная — для
    строк лога, которые пишет сам обработчик запроса. ``request.state`` — для
    строки, которую пишет middleware: обработчик выполняется в отдельной задаче,
    и установленная в нём контекстная переменная до middleware не доезжает,
    а состояние запроса общее.
    """
    actor = f"{user_id}/{role}"
    _actor.set(actor)
    if request is not None:
        request.state.actor = actor


def mask_email(email: str) -> str:
    """Почта для лога: первая буква и домен. Для разбора перебора этого хватает."""
    address = (email or "").strip()
    if "@" not in address:
        return "***"
    name, domain = address.split("@", 1)
    return f"{name[:1]}***@{domain}"


class ContextFilter(logging.Filter):
    """Добавляет к записи номер запроса и того, кто его сделал."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id.get()
        record.actor = _actor.get()
        return True


#: Фильтр висит и на логгере приложения, а не только на обработчике: иначе номер
#: запроса появлялся бы в записи лишь по дороге в наш обработчик, и всё, что
#: читает записи иначе (тесты, чужой обработчик), его бы не увидело.
logger.addFilter(ContextFilter())


class TextFormatter(logging.Formatter):
    # Время в UTC, как и все отметки в базе: иначе строку лога и запись журнала
    # синхронизаций пришлось бы в уме сдвигать на часовой пояс машины. Буква Z
    # стоит затем, чтобы это было видно, а не предполагалось.
    converter = time.gmtime
    default_time_format = "%Y-%m-%d %H:%M:%S"

    def format(self, record: logging.LogRecord) -> str:
        head = (
            f"{self.formatTime(record)}Z {record.levelname:<7} {record.name} "
            f"req={getattr(record, 'request_id', '-')} "
            f"user={getattr(record, 'actor', '-')} {record.getMessage()}"
        )
        extra = getattr(record, "fields", None)
        if extra:
            head += " " + " ".join(f"{key}={value}" for key, value in extra.items())
        if record.exc_info:
            head += "\n" + self.formatException(record.exc_info)
        return head


class JsonFormatter(logging.Formatter):
    """Строка-объект на событие: для сборщика логов, а не для глаз."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "actor": getattr(record, "actor", "-"),
            "message": record.getMessage(),
        }
        payload.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


#: Имя своего обработчика. По нему он и снимается при повторной настройке —
#: чужие обработчики не трогаются: приложение собирают и внутри чужого процесса
#: (тесты, отладчик), и выбрасывать там чей-то обработчик значит молча лишить
#: хозяина процесса его собственных логов.
HANDLER_NAME = "raketa-stream"


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Настраивает корневой обработчик. Зовётся один раз при сборке приложения."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if fmt.lower() == "json" else TextFormatter())
    handler.addFilter(ContextFilter())
    handler.set_name(HANDLER_NAME)

    root = logging.getLogger()
    # Свой прежний обработчик снимается: иначе при повторной сборке приложения
    # (тесты, перезапуск с --reload) строки задвоились бы.
    for existing in [h for h in root.handlers if h.get_name() == HANDLER_NAME]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Журнал обращений uvicorn выключается: он пишет то же самое, но без номера
    # запроса и без того, кто его сделал, — две строки на запрос не нужны.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False

    # Журнал обращений HTTP-клиента — тоже. Он печатает строку запроса целиком,
    # вместе с адресом, а в адресе входящего вебхука (задача G-16) лежит ключ:
    # маскировать его у себя и тут же отдавать чужой библиотеке незачем.
    # Свои обращения наружу пишутся своими событиями.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def event(message: str, level: int = logging.INFO, **fields: object) -> None:
    """Событие с полями: вход, синхронизация, загрузка файла.

    Поля пишутся отдельно от текста, чтобы в JSON они стали полями объекта,
    а не частью строки, которую потом разбирают регулярным выражением.
    ``exception=True`` добавляет трассировку текущей ошибки.
    """
    with_trace = bool(fields.pop("exception", False))
    logger.log(level, message, exc_info=with_trace, extra={"fields": fields})


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Строка на запрос и номер запроса в заголовке ответа."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        token = _request_id.set(
            incoming if SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex[:12]
        )
        actor_token = _actor.set("-")
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Необработанная ошибка: без этой записи в логе остаётся трассировка
            # без пути, номера запроса и того, кто его сделал.
            event(
                "запрос упал",
                level=logging.ERROR,
                method=request.method,
                path=request.url.path,
                ms=f"{(time.perf_counter() - started) * 1000:.0f}",
                exception=True,
            )
            raise
        else:
            # Кто это был, становится известно в проверке доступа — уже внутри
            # обработчика; сюда это доезжает через состояние запроса.
            actor = getattr(request.state, "actor", None)
            if actor:
                _actor.set(actor)
            # Строка запроса не пишется: в ней поисковые запросы и фильтры.
            event(
                f"{request.method} {request.url.path}",
                level=logging.WARNING if response.status_code >= 500 else logging.INFO,
                status=response.status_code,
                ms=f"{(time.perf_counter() - started) * 1000:.0f}",
            )
            response.headers[REQUEST_ID_HEADER] = _request_id.get()
            return response
        finally:
            _request_id.reset(token)
            _actor.reset(actor_token)
