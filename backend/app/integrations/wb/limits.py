"""Лимиты запросов, повторы и паузы для WB API (задача E-5).

Числа здесь не выдуманы — они из документации WB (`dev.wildberries.ru`,
раздел «Информация об API» и описание метода «Продажи»):

* скорость ограничена алгоритмом token bucket, лимит задан **на профиль
  продавца и на метод**; у `/api/v1/supplier/sales` это **1 запрос в минуту**
  (у базового токена без секрета — 1 запрос в 2 часа);
* превышение даёт **429**, и в ответе приезжают заголовки: `X-Ratelimit-Retry`
  (через сколько секунд можно повторить — «если сделаете попытку раньше, вы
  всё равно будете получать 429»), `X-Ratelimit-Reset` и `X-Ratelimit-Limit`;
* в обычных ответах есть `X-Ratelimit-Remaining` — сколько запросов можно
  сделать сейчас без паузы;
* **запрос с ответом 4XX считается за десять запросов.** Поэтому повторять
  ошибку клиента нельзя: повтор неверного запроса тратит лимит в десять раз
  быстрее и всё равно не починит ни токен, ни параметры.

Отсюда три решения:

* **Ждать столько, сколько сказал сервер.** Своя формула отступа применяется
  только там, где сервер ничего не сказал: сеть не ответила или ответ 5XX
  пришёл без заголовков.
* **Повторять только то, что может пройти со второй попытки**: сетевую ошибку,
  429 и 5XX. 401, 400, 403 — отказ сразу, с текстом в журнал.
* **Паузу держать по паре (кабинет, метод)**, потому что лимит именно такой:
  два кабинета — это два профиля продавца, и ждать друг друга им незачем.

Проверить это на боевом ключе пока нельзя (вопрос Q-3, задача E-3), поэтому
весь путь — паузы, повторы, разбор заголовков — закрыт тестами на подставном
транспорте: они отвечают теми же кодами и заголовками, что описаны в документации.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

#: Через сколько секунд можно повторить запрос после 429 — главный заголовок.
RETRY_HEADER = "X-Ratelimit-Retry"
#: Через сколько секунд восстановится допустимый всплеск.
RESET_HEADER = "X-Ratelimit-Reset"
#: Сколько запросов можно сделать сейчас без паузы (во всех ответах, кроме 429).
REMAINING_HEADER = "X-Ratelimit-Remaining"
#: Стандартный заголовок — на случай, если WB ответит им, а не своим.
STANDARD_RETRY_HEADER = "Retry-After"

#: Коды, после которых повтор имеет смысл: лимит и внутренние ошибки сервиса.
#: Документация про 5XX говорит прямо: «Повторите запрос позже».
TOO_MANY = 429


@dataclass(frozen=True)
class RetryPolicy:
    """Сколько раз пробовать, сколько ждать и когда сдаваться."""

    #: Попыток на один запрос, включая первую.
    attempts: int = 4
    #: Основание отступа: 2 с, 4 с, 8 с… Применяется, когда сервер не сказал.
    backoff_seconds: float = 2.0
    #: Предел одной паузы. Чтобы ответ «подождите час» не держал прогон час:
    #: лучше упасть с понятным текстом (о сбое сообщат алерты, задача G-16),
    #: чем занимать блокировку загрузки и ждать молча.
    max_wait_seconds: float = 120.0
    #: Минимальная пауза между запросами одной пары (кабинет, метод).
    #: По документации для «Продаж» — 60 секунд.
    min_interval_seconds: float = 60.0
    #: Предел страниц на один метод. 80 000 строк на страницу: двадцати хватает
    #: на 1,6 млн строк. Предел нужен не ради скорости, а чтобы цикл с курсором
    #: не стал бесконечным, если сервер начнёт отдавать одно и то же.
    max_pages: int = 20

    def backoff(self, attempt: int) -> float:
        """Отступ для попытки: 2, 4, 8… не больше предела одной паузы."""
        return min(self.backoff_seconds * (2 ** max(0, attempt - 1)), self.max_wait_seconds)


def should_retry(status: int) -> bool:
    """Повторять только лимит и ошибки сервиса; 4XX — отказ сразу."""
    return status == TOO_MANY or status >= 500


def header(headers: dict | None, name: str) -> object | None:
    """Заголовок без оглядки на регистр.

    Нужно именно так: HTTP-клиент отдаёт заголовки приведёнными к нижнему
    регистру, а в документации они написаны как ``X-Ratelimit-Retry``. Поиск
    по точному написанию молча не находил бы названный сервером срок —
    и пауза считалась бы дважды.
    """
    if not headers:
        return None
    if name in headers:
        return headers[name]
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return value
    return None


def _positive_number(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def server_wait(headers: dict | None, policy: RetryPolicy) -> float | None:
    """Срок, названный сервером: `X-Ratelimit-Retry`, `Retry-After`, `-Reset`.

    ``None`` — сервер ничего не сказал (сеть не ответила, 5XX без заголовков).
    Это важно отличать: названный сервером срок — это и есть ответ про его
    собственный счётчик, и добавлять к нему свою минуту незачем.
    """
    for name in (RETRY_HEADER, STANDARD_RETRY_HEADER, RESET_HEADER):
        value = _positive_number(header(headers, name))
        if value is not None:
            return min(value, policy.max_wait_seconds)
    return None


def wait_for(headers: dict | None, policy: RetryPolicy, attempt: int) -> float:
    """Сколько ждать перед повтором.

    Сначала то, что сказал сервер, и только потом своя формула: раньше
    названного сервером срока повтор всё равно вернёт 429.
    """
    told = server_wait(headers, policy)
    return told if told is not None else policy.backoff(attempt)


def remaining_from(headers: dict | None) -> int | None:
    """Сколько запросов можно сделать без паузы. ``None`` — сервер не сказал."""
    value = header(headers, REMAINING_HEADER)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class Traffic:
    """Что стоило загрузить кабинет — для журнала прогона и для лога."""

    requests: int = 0
    retries: int = 0
    pages: int = 0
    #: Строки, пришедшие второй раз: курсор пагинации включает последнюю строку
    #: предыдущей страницы, и без отбрасывания такие строки сложились бы дважды.
    duplicates: int = 0
    waited_seconds: float = 0.0
    #: Данные загружены не полностью: страницы кончились раньше данных.
    truncated: bool = False

    def note(self) -> str:
        """Короткая строка для журнала. Пусто — говорить не о чем."""
        parts = []
        if self.pages > 1:
            parts.append(f"страниц {self.pages}")
        if self.duplicates:
            parts.append(f"повторных строк {self.duplicates}")
        if self.retries:
            parts.append(f"повторов запроса {self.retries}")
        if self.waited_seconds >= 1:
            parts.append(f"ожидание лимита {self.waited_seconds:.0f} с")
        return ", ".join(parts)


class Throttle:
    """Паузы между запросами по паре (кабинет, метод).

    Лимит WB задан на профиль продавца и на метод, поэтому ключ именно такой:
    общая пауза на весь клиент заставила бы второй кабинет ждать первого,
    а это другой профиль с другим лимитом.

    Если сервер сказал, что всплеск ещё есть (``X-Ratelimit-Remaining`` больше
    нуля), пауза не нужна. Если не сказал — ждём: лишняя минута дешевле ответа
    429, который по документации считается за десять запросов.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.clock = clock
        self.sleep = sleep
        self._last: dict[tuple[str, str], float] = {}
        self._remaining: dict[tuple[str, str], int | None] = {}

    def before(self, key: tuple[str, str]) -> float:
        """Ждёт, если нужно. Возвращает, сколько проспали."""
        last = self._last.get(key)
        if last is None or self.min_interval_seconds <= 0:
            return 0.0
        remaining = self._remaining.get(key)
        if remaining is not None and remaining > 0:
            return 0.0
        gap = self.min_interval_seconds - (self.clock() - last)
        if gap <= 0:
            return 0.0
        self.sleep(gap)
        return gap

    def after(self, key: tuple[str, str], headers: dict | None = None) -> None:
        self._last[key] = self.clock()
        self._remaining[key] = remaining_from(headers)

    def forget(self, key: tuple[str, str]) -> None:
        """После 429 о всплеске известно только одно: его нет."""
        self._remaining[key] = 0

    def cleared(self, key: tuple[str, str]) -> None:
        """Сервер назвал срок, и его выждали — своей паузы больше не нужно.

        Без этого пауза считалась бы дважды: сначала столько, сколько сказал
        сервер, а потом ещё до конца минуты, которую отмеряет клиент. Срок
        от сервера точнее: он про его собственный счётчик.
        """
        self._remaining[key] = 1


@dataclass
class Dedup:
    """Отбрасывает строки, пришедшие второй раз (контроль дублей, задача E-5).

    Курсор пагинации у WB — ``lastChangeDate`` последней строки предыдущей
    страницы, и строки с ровно этим временем приезжают снова. Для продаж это
    не безобидно: строка продажи — единица товара, и сложенная дважды она даёт
    завышенный спрос, а по нему считается закупка.

    Ключ — ``srid``: документация прямо советует определять заказ по нему.
    Если его нет, берётся ``saleID``; если нет и его, строка пропускается
    через всё, что у неё есть, — повтор такой строки не узнать.
    """

    seen: set[str] = field(default_factory=set)
    duplicates: int = 0

    def is_new(self, key: str | None) -> bool:
        if not key:
            return True
        if key in self.seen:
            self.duplicates += 1
            return False
        self.seen.add(key)
        return True
