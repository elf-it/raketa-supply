"""Клиент Wildberries.

Два клиента за одним интерфейсом:

* :class:`FixtureWbClient` — читает локальные JSON-файлы, на нём идёт разработка
  и тесты, ключи не нужны;
* :class:`HttpWbClient` — обращение к statistics-api WB.

HTTP-клиент написан по публичной документации, но **не проверен на боевых ключах**:
кабинеты и токены заказчик ещё не передал (вопрос Q-3, задача E-3 в бэклоге).
Пока `WB_CLIENT=fixtures`.

Лимиты, повторы и отбрасывание повторных строк — в `limits.py` (задача E-5).

Что в документации WB изменилось с тех пор, как этот клиент был написан,
и чего он поэтому не умеет (задача E-8):

* **Остатков** в нынешней документации нет по пути `/api/v1/supplier/stocks`:
  складские остатки отдаёт отчёт `/api/v1/warehouse_remains` — он
  асинхронный, в три шага (создать задание, дождаться готовности, скачать).
  Прежний путь, возможно, ещё работает, но на него больше нельзя ссылаться
  как на документированный.
* **Заказов** здесь нет вовсе: метод «Продажи» отдаёт продажи и возвраты,
  а заказы — это `/api/v1/supplier/orders`, отдельный метод со своим лимитом.
* **`dateFrom` у WB в московском времени** (UTC+3), а не в UTC.

Проверять и править это вслепую, без ключей, значит угадывать дважды.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from .limits import TOO_MANY, Dedup, RetryPolicy, Throttle, Traffic, server_wait, should_retry

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Пути методов statistics-api.
SALES_PATH = "/api/v1/supplier/sales"
STOCKS_PATH = "/api/v1/supplier/stocks"


@dataclass(frozen=True)
class WbCard:
    """Карточка номенклатуры кабинета."""

    nm_id: str
    vendor_code: str
    title: str
    category: str | None = None
    barcode: str | None = None


@dataclass(frozen=True)
class WbSaleRow:
    """Дневная строка продаж/заказов."""

    nm_id: str
    day: date
    order_units: float = 0.0
    buyout_units: float = 0.0
    sales_units: float = 0.0


@dataclass(frozen=True)
class WbStockRow:
    """Остаток на дату."""

    nm_id: str
    day: date
    quantity: float = 0.0


class WbClient(Protocol):
    def fetch_cards(self, store_code: str) -> list[WbCard]: ...

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]: ...

    def fetch_stocks(self, store_code: str, date_from: date) -> list[WbStockRow]: ...


def _parse_day(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _sale_key(item: dict) -> str | None:
    """Чем узнаётся та же самая строка продажи.

    Документация советует определять заказ по ``srid``; ``saleID`` берётся,
    когда ``srid`` не пришёл. Строку без обоих узнать нечем — она пройдёт.
    """
    for name in ("srid", "saleID"):
        value = item.get(name)
        if value:
            return f"{name}:{value}"
    return None


def _sale_row(item: dict) -> WbSaleRow:
    # saleID, начинающийся на S, — продажа; на R — возврат.
    sign = -1.0 if str(item.get("saleID", "")).startswith("R") else 1.0
    return WbSaleRow(
        nm_id=str(item.get("nmId", "")),
        day=_parse_day(item["date"]),
        # Заказы живут в другом методе (`/api/v1/supplier/orders`), здесь
        # только продажи и возвраты — задача E-8.
        order_units=0.0,
        buyout_units=sign,
        sales_units=sign,
    )


class FixtureWbClient:
    """Фикстурный клиент: `fixtures/<store_code>.json`."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or FIXTURES_DIR

    def _load(self, store_code: str) -> dict:
        path = self.directory / f"{store_code}.json"
        if not path.exists():
            return {"cards": [], "sales": [], "stocks": []}
        return json.loads(path.read_text(encoding="utf-8"))

    def fetch_cards(self, store_code: str) -> list[WbCard]:
        return [WbCard(**row) for row in self._load(store_code).get("cards", [])]

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]:
        rows = []
        for row in self._load(store_code).get("sales", []):
            day = _parse_day(row["day"])
            if day < date_from:
                continue
            rows.append(
                WbSaleRow(
                    nm_id=str(row["nm_id"]),
                    day=day,
                    order_units=row.get("order_units", 0.0),
                    buyout_units=row.get("buyout_units", 0.0),
                    sales_units=row.get("sales_units", 0.0),
                )
            )
        return rows

    def fetch_stocks(self, store_code: str, date_from: date) -> list[WbStockRow]:
        rows = []
        for row in self._load(store_code).get("stocks", []):
            day = _parse_day(row["day"])
            if day < date_from:
                continue
            rows.append(
                WbStockRow(nm_id=str(row["nm_id"]), day=day, quantity=row.get("quantity", 0.0))
            )
        return rows


class WbApiError(RuntimeError):
    """Ответ WB, после которого загрузка кабинета продолжаться не может.

    Отдельный тип нужен, чтобы в журнал прогона попал код ответа, а не
    трассировка чужой библиотеки: 401 (токен), 400 (параметры) и 429 (лимит)
    разбираются по-разному, и человеку это видно сразу.
    """

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        where = f"WB ответил {status}" if status else "WB не ответил"
        super().__init__(f"{where}: {detail}" if detail else where)


class HttpWbClient:
    """Реальный клиент statistics-api WB. Не проверен на боевых ключах (E-3).

    Повторы, паузы по лимиту и отбрасывание повторных строк — в ``limits.py``;
    числа взяты из документации WB. Часы и сон подставляются снаружи, иначе
    тесты на паузы шли бы столько же, сколько сами паузы.
    """

    def __init__(
        self,
        tokens: dict[str, str],
        base_url: str,
        timeout: float = 60.0,
        policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transport: Any | None = None,
    ) -> None:
        self.tokens = tokens
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.policy = policy or RetryPolicy()
        self.sleep = sleep
        self.transport = transport
        self.throttle = Throttle(self.policy.min_interval_seconds, clock=clock, sleep=sleep)
        self.traffic = Traffic()

    def take_traffic(self) -> Traffic:
        """Отдаёт счётчики и обнуляет их — по кабинету, а не за всё время.

        Намеренно не в протоколе ``WbClient``: фикстурному клиенту и подставным
        клиентам в тестах рассказывать не о чем, а требовать от них этот метод
        значило бы писать его ради самого протокола.
        """
        traffic, self.traffic = self.traffic, Traffic()
        return traffic

    def _token(self, store_code: str) -> str:
        token = self.tokens.get(store_code)
        if not token:
            raise RuntimeError(
                f"нет токена для кабинета {store_code!r}. "
                'Задайте WB_TOKENS="<код магазина>=<токен>,..." в окружении'
            )
        return token

    @staticmethod
    def _detail(response) -> str:
        """Текст отказа из тела ответа. В 404 и 429 WB кладёт туда подсказку."""
        try:
            body = response.json()
        except ValueError:
            return response.text[:200].strip()
        if isinstance(body, dict):
            for name in ("detail", "details", "title", "message", "errorText"):
                value = body.get(name)
                if value:
                    return str(value)[:300]
        return ""

    def _get(self, store_code: str, path: str, params: dict) -> list[dict]:
        """Запрос с паузой по лимиту и повторами. Ошибка — ``WbApiError``."""
        import httpx

        key = (store_code, path)
        headers = {"Authorization": self._token(store_code)}
        url = f"{self.base_url}{path}"
        last: WbApiError | Exception | None = None
        #: Назвал ли сервер срок повтора сам: тогда своя пауза уже не нужна.
        told_by_server = False

        for attempt in range(1, self.policy.attempts + 1):
            self.traffic.waited_seconds += self.throttle.before(key)
            try:
                with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                    response = client.get(url, params=params, headers=headers)
            except httpx.HTTPError as error:
                # Сеть не ответила — сервер ничего не сказал, ждём по формуле.
                self.throttle.after(key)
                last = error
                pause = self.policy.backoff(attempt)
                told_by_server = False
            else:
                self.traffic.requests += 1
                self.throttle.after(key, dict(response.headers))
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        raise WbApiError(200, "ответ не разобрать как JSON") from None
                    return payload if isinstance(payload, list) else []
                detail = self._detail(response)
                if not should_retry(response.status_code):
                    # 4XX повторять нельзя: по документации такой ответ считается
                    # за десять запросов, а токен или параметры повтор не починит.
                    raise WbApiError(response.status_code, detail)
                if response.status_code == TOO_MANY:
                    self.throttle.forget(key)
                last = WbApiError(response.status_code, detail)
                told = server_wait(dict(response.headers), self.policy)
                told_by_server = told is not None
                pause = told if told_by_server else self.policy.backoff(attempt)

            if attempt >= self.policy.attempts:
                break
            self.traffic.retries += 1
            self.sleep(pause)
            self.traffic.waited_seconds += pause
            if told_by_server:
                # Срок назвал сервер, и его выждали: добавлять к нему свою
                # минуту значило бы ждать дважды за одно и то же.
                self.throttle.cleared(key)

        tail = f" (попыток: {self.policy.attempts})"
        if isinstance(last, WbApiError):
            raise WbApiError(last.status, f"{last.detail}{tail}".strip())
        raise WbApiError(0, f"{type(last).__name__}: {last}{tail}")

    def fetch_cards(self, store_code: str) -> list[WbCard]:
        # Карточки живут в content-api, а не в statistics-api; подключается вместе
        # с реальными ключами. Пока справочник ведётся в системе вручную.
        raise NotImplementedError("загрузка карточек из content-api WB не подключена, задача E-3")

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]:
        """Все продажи и возвраты с ``date_from`` — сколько бы страниц ни ушло.

        Пагинация по документации WB: в одном ответе условно не больше
        80 000 строк, и во втором и далее запросе в ``dateFrom`` передаётся
        **полное значение ``lastChangeDate`` последней строки** предыдущего
        ответа; пустой массив означает, что всё выгружено.

        Курсор передаётся строкой как пришёл, без разбора в дату: время у WB
        московское (UTC+3) и с точностью до миллисекунд, и любое приведение
        типа — это шанс сдвинуть курсор на час и получить сутки чужих строк
        или потерять свои.

        Повторные строки отбрасываются по ``srid``: курсор включает последнюю
        строку предыдущей страницы, а строка продажи — это единица товара,
        и сложенная дважды она завысила бы спрос, по которому считается закупка.
        """
        cursor = date_from.isoformat()
        dedup = Dedup()
        rows: list[WbSaleRow] = []

        for page in range(1, self.policy.max_pages + 1):
            raw = self._get(store_code, SALES_PATH, {"dateFrom": cursor, "flag": 0})
            self.traffic.pages = page
            if not raw:
                # Пустой массив — всё уже выгружено.
                break
            added = 0
            for item in raw:
                if not dedup.is_new(_sale_key(item)):
                    continue
                rows.append(_sale_row(item))
                added += 1
            following = str(raw[-1].get("lastChangeDate") or "")
            if not following or following == cursor or added == 0:
                # Курсор не двигается или страница целиком повторная: дальше
                # будет то же самое, и цикл стал бы бесконечным.
                break
            cursor = following
        else:
            # Страницы кончились раньше данных. Записанные факты верны,
            # но картина неполная, и выглядеть она не должна полной.
            self.traffic.truncated = True

        self.traffic.duplicates += dedup.duplicates
        return rows

    def fetch_stocks(self, store_code: str, date_from: date) -> list[WbStockRow]:
        raw = self._get(store_code, STOCKS_PATH, {"dateFrom": date_from.isoformat()})
        return [
            WbStockRow(
                nm_id=str(item.get("nmId", "")),
                day=_parse_day(item.get("lastChangeDate", date_from.isoformat())),
                quantity=float(item.get("quantity", 0) or 0),
            )
            for item in raw
        ]


def make_client(
    kind: str,
    tokens: dict[str, str],
    base_url: str,
    fixtures_dir: Path | None = None,
    policy: RetryPolicy | None = None,
) -> WbClient:
    """Клиент по настройке. ``fixtures_dir`` — где лежат выгрузки для ``fixtures``.

    Пусто — рядом с кодом. Отдельная папка нужна там, где сгенерированные
    выгрузки не должны лежать в репозитории: у сида есть такой же ключ,
    и без этой настройки загрузка искала бы их не там, где он их положил.
    """
    if kind == "http":
        return HttpWbClient(tokens=tokens, base_url=base_url, policy=policy)
    return FixtureWbClient(fixtures_dir)


def policy_from(settings) -> RetryPolicy:
    """Политика повторов и паузы из настроек приложения (задача E-5)."""
    return RetryPolicy(
        attempts=settings.wb_max_attempts,
        backoff_seconds=settings.wb_retry_backoff_seconds,
        max_wait_seconds=settings.wb_max_wait_seconds,
        min_interval_seconds=settings.wb_min_interval_seconds,
        max_pages=settings.wb_max_pages,
    )
