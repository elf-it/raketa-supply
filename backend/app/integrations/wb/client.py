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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .limits import TOO_MANY, Dedup, RetryPolicy, Throttle, Traffic, server_wait, should_retry

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Пути методов. Продажи и заказы — категория токена «Статистика»,
#: отчёт об остатках — «Аналитика» (задача E-8).
SALES_PATH = "/api/v1/supplier/sales"
ORDERS_PATH = "/api/v1/supplier/orders"
REMAINS_PATH = "/api/v1/warehouse_remains"
#: Прежний путь остатков. В нынешней документации его нет; оставлен как запасной
#: до первой проверки на боевом ключе (``WB_STOCKS_SOURCE=legacy``).
STOCKS_PATH = "/api/v1/supplier/stocks"

#: Часовой пояс, в котором WB принимает ``dateFrom`` и отдаёт даты, — Москва.
MSK = timezone(timedelta(hours=3))

#: Строки отчёта об остатках, которые складом не являются: это товар в пути
#: и общий итог. Сложить их вместе с остальными значило бы посчитать остаток
#: дважды и прибавить к нему то, что до склада ещё не доехало.
REMAINS_TOTAL = "Всего находится на складах"
REMAINS_IN_TRANSIT = ("В пути",)


def msk_date_of(moment: datetime) -> date:
    """Московская дата момента времени.

    Отдельной функцией, потому что проверить это можно только на заданном
    моменте: разница между местной и московской датой видна лишь в узком окне
    суток, и тест на «сейчас» молчал бы почти всегда.
    """
    return moment.astimezone(MSK).date()


def msk_today() -> date:
    """Сегодняшняя дата по Москве.

    Остаток приезжает срезом «на сейчас», без даты, и ложится в неделю этого
    дня. На машине в другом поясе местная дата может отличаться от московской,
    а в ночь с воскресенья на понедельник — ещё и неделей.
    """
    return msk_date_of(datetime.now(MSK))


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
class WbOrderRow:
    """Дневная строка заказов. Отменённые заказы сюда не попадают."""

    nm_id: str
    day: date
    order_units: float = 1.0


@dataclass(frozen=True)
class WbStockRow:
    """Остаток на дату."""

    nm_id: str
    day: date
    quantity: float = 0.0


class WbClient(Protocol):
    def fetch_cards(self, store_code: str) -> list[WbCard]: ...

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]: ...

    def fetch_orders(self, store_code: str, date_from: date) -> list[WbOrderRow]: ...

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
        # Заказы живут в отдельном методе (`fetch_orders`): этот отдаёт
        # продажи и возвраты.
        order_units=0.0,
        buyout_units=sign,
        sales_units=sign,
    )


def _order_row(item: dict) -> WbOrderRow | None:
    """Строка заказа. ``None`` — отменённый заказ: спросом он не был.

    Отменённые приходят той же выгрузкой с ``isCancel``. Считать их спросом
    нельзя: по спросу считается закупка, и отменённые заказы раздули бы её.
    Недельные значения перезаписываются каждым прогоном целиком, поэтому
    заказ, отменённый после загрузки, перестанет считаться на следующем
    прогоне — если неделя ещё попадает в его глубину.
    """
    if item.get("isCancel"):
        return None
    return WbOrderRow(nm_id=str(item.get("nmId", "")), day=_parse_day(item["date"]))


def _stock_quantity(warehouses: list[dict] | None) -> float:
    """Остаток позиции из разбивки по складам отчёта.

    В списке лежат не только склады: есть общий итог «Всего находится
    на складах» и строки «В пути…». Сумма всего подряд дала бы двойной остаток
    плюс товар, который до склада ещё не доехал, — а остаток в расчёте
    умножается на недели покрытия, и ошибка вышла бы заметной в заказе.

    Поэтому берётся итоговая строка, а если её нет — сумма складов без «в пути».
    """
    rows = warehouses or []
    for row in rows:
        if str(row.get("warehouseName", "")).strip() == REMAINS_TOTAL:
            return float(row.get("quantity", 0) or 0)
    total = 0.0
    for row in rows:
        name = str(row.get("warehouseName", "")).strip()
        if name.startswith(REMAINS_IN_TRANSIT):
            continue
        total += float(row.get("quantity", 0) or 0)
    return total


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

    def fetch_orders(self, store_code: str, date_from: date) -> list[WbOrderRow]:
        """В выгрузках заказы лежат в строке продаж полем ``order_units``.

        Отдельного метода для них здесь нет намеренно: выгрузки — это формат
        сида и тестов, и делить одну строку на две ради сходства с живым API
        значило бы переписать сид под то, что в живом API ещё не проверено.
        """
        return []

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
        stocks_source: str = "report",
    ) -> None:
        self.tokens = tokens
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.policy = policy or RetryPolicy()
        self.stocks_source = (stocks_source or "report").strip().lower()
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

    def _fetch(
        self,
        store_code: str,
        path: str,
        params: dict | None = None,
        interval: float | None = None,
        key_path: str | None = None,
    ) -> Any:
        """Запрос с паузой по лимиту и повторами. Ошибка — ``WbApiError``.

        Возвращает разобранный ответ как есть: списком отдают выгрузки строк,
        объектом — шаги отчёта об остатках. ``interval`` переопределяет паузу:
        у проверки статуса отчёта лимит свой, 1 запрос в 5 секунд.
        ``key_path`` нужен там, где в пути стоит номер задания: лимит задан
        на метод, и каждое задание не должно получать свою порцию лимита.
        """
        import httpx

        key = (store_code, key_path or path)
        headers = {"Authorization": self._token(store_code)}
        url = f"{self.base_url}{path}"
        last: WbApiError | Exception | None = None
        #: Назвал ли сервер срок повтора сам: тогда своя пауза уже не нужна.
        told_by_server = False

        for attempt in range(1, self.policy.attempts + 1):
            self.traffic.waited_seconds += self.throttle.before(key, interval)
            try:
                with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                    response = client.get(url, params=params or {}, headers=headers)
            except httpx.HTTPError as error:
                # Сеть не ответила — сервер ничего не сказал, ждём по формуле.
                self.throttle.after(key)
                last = error
                pause = self.policy.backoff(attempt)
                told_by_server = False
            else:
                self.traffic.requests += 1
                self.throttle.after(key, dict(response.headers))
                if response.status_code == 204:
                    # «Нет данных» у готового отчёта: это не ошибка.
                    return []
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        raise WbApiError(200, "ответ не разобрать как JSON") from None
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

    def _walk(self, store_code: str, path: str, date_from: date, make_row) -> list:
        """Обход страниц выгрузки с курсором — один для продаж и заказов.

        Пагинация у обоих методов описана одинаково: в одном ответе условно
        не больше 80 000 строк, во втором и далее запросе в ``dateFrom`` идёт
        **полное значение ``lastChangeDate`` последней строки** предыдущего
        ответа, пустой массив означает, что всё выгружено.

        Курсор передаётся строкой как пришёл, без разбора в дату: время у WB
        московское (UTC+3) и с точностью до миллисекунд, и любое приведение
        типа — это шанс сдвинуть курсор и получить сутки чужих строк или
        потерять свои. Первая страница спрашивается московской датой.

        Повторные строки отбрасываются по ``srid``: курсор включает последнюю
        строку предыдущей страницы, а строка здесь — единица товара, и сложенная
        дважды она завысила бы спрос, по которому считается закупка.

        ``make_row`` возвращает ``None`` для строки, которую считать не нужно
        (отменённый заказ): она всё равно учитывается в контроле дублей —
        иначе страница из одних отменённых выглядела бы как «ничего нового»
        и обход встал бы раньше времени.
        """
        cursor = date_from.isoformat()
        dedup = Dedup()
        rows: list = []

        for _page in range(1, self.policy.max_pages + 1):
            raw = self._fetch(store_code, path, {"dateFrom": cursor, "flag": 0})
            # Страницы считаются суммой по всем выгрузкам кабинета: в журнале
            # нужна цена прогона, а не глубина одного обхода.
            self.traffic.pages += 1
            if not raw:
                # Пустой массив — всё уже выгружено.
                break
            fresh = 0
            for item in raw:
                if not dedup.is_new(_sale_key(item)):
                    continue
                fresh += 1
                row = make_row(item)
                if row is not None:
                    rows.append(row)
            following = str(raw[-1].get("lastChangeDate") or "")
            if not following or following == cursor or fresh == 0:
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

    def fetch_sales(self, store_code: str, date_from: date) -> list[WbSaleRow]:
        """Продажи и возвраты с ``date_from`` — сколько бы страниц ни ушло."""
        return self._walk(store_code, SALES_PATH, date_from, _sale_row)

    def fetch_orders(self, store_code: str, date_from: date) -> list[WbOrderRow]:
        """Заказы с ``date_from``. Отменённые не считаются спросом.

        Отдельный метод со своим лимитом: «Продажи» отдают продажи и возвраты,
        а в расчёте спроса участвуют и заказы.
        """
        return self._walk(store_code, ORDERS_PATH, date_from, _order_row)

    def fetch_stocks(self, store_code: str, date_from: date) -> list[WbStockRow]:
        """Остатки: отчётом (по документации) или прежним путём.

        Выбор — настройкой ``WB_STOCKS_SOURCE``. По умолчанию отчёт: это то,
        что описано в документации сейчас. Прежний путь оставлен запасным,
        потому что ни один из двух не проверен на боевом ключе, а отчёту нужен
        токен категории «Аналитика» — его у кабинета может не оказаться.
        """
        if self.stocks_source == "legacy":
            return self._stocks_legacy(store_code, date_from)
        return self._stocks_report(store_code)

    def _stocks_legacy(self, store_code: str, date_from: date) -> list[WbStockRow]:
        raw = self._fetch(store_code, STOCKS_PATH, {"dateFrom": date_from.isoformat()})
        return [
            WbStockRow(
                nm_id=str(item.get("nmId", "")),
                day=_parse_day(item.get("lastChangeDate", date_from.isoformat())),
                quantity=float(item.get("quantity", 0) or 0),
            )
            for item in raw or []
        ]

    def _stocks_report(self, store_code: str) -> list[WbStockRow]:
        """Отчёт об остатках: создать задание, дождаться, скачать.

        Три шага и три разных лимита (создание — 1 запрос в минуту, статус —
        1 запрос в 5 секунд, скачивание — 1 запрос в минуту), поэтому пауза
        у проверки статуса своя.

        Срез приезжает без даты — «на сейчас», — и ложится в неделю
        сегодняшнего московского дня: дата у WB московская, и на машине
        в другом поясе срез иначе попал бы не в ту неделю.
        """
        created = self._fetch(store_code, REMAINS_PATH, {"groupByNm": "true", "locale": "ru"})
        task_id = str(((created or {}).get("data") or {}).get("taskId") or "").strip()
        if not task_id:
            raise WbApiError(200, "отчёт об остатках не создан: в ответе нет taskId")

        self._await_report(store_code, task_id)

        rows = self._fetch(
            store_code,
            f"{REMAINS_PATH}/tasks/{task_id}/download",
            key_path=f"{REMAINS_PATH}/download",
        )
        day = msk_today()
        out: list[WbStockRow] = []
        for item in rows or []:
            nm_id = str(item.get("nmId", ""))
            if not nm_id:
                continue
            out.append(
                WbStockRow(
                    nm_id=nm_id,
                    day=day,
                    quantity=_stock_quantity(item.get("warehouses")),
                )
            )
        return out

    def _await_report(self, store_code: str, task_id: str) -> None:
        """Ждёт готовности отчёта. Статусы, кроме ``done``, документация
        не перечисляет, поэтому всё прочее считается «ещё не готов» —
        с отдельным пределом ожидания, иначе зависшее задание держало бы
        прогон и блокировку загрузки до бесконечности.
        """
        path = f"{REMAINS_PATH}/tasks/{task_id}/status"
        interval = self.policy.status_interval_seconds
        checks = max(1, int(self.policy.report_wait_seconds // max(interval, 1)))
        status = "?"
        for _ in range(checks):
            answer = self._fetch(
                store_code, path, interval=interval, key_path=f"{REMAINS_PATH}/status"
            )
            status = str(((answer or {}).get("data") or {}).get("status") or "").strip()
            if status == "done":
                return
        raise WbApiError(
            0,
            f"отчёт об остатках не готов за {self.policy.report_wait_seconds:.0f} с "
            f"(последний статус: {status or 'неизвестен'})",
        )


def make_client(
    kind: str,
    tokens: dict[str, str],
    base_url: str,
    fixtures_dir: Path | None = None,
    policy: RetryPolicy | None = None,
    stocks_source: str = "report",
) -> WbClient:
    """Клиент по настройке. ``fixtures_dir`` — где лежат выгрузки для ``fixtures``.

    Пусто — рядом с кодом. Отдельная папка нужна там, где сгенерированные
    выгрузки не должны лежать в репозитории: у сида есть такой же ключ,
    и без этой настройки загрузка искала бы их не там, где он их положил.
    """
    if kind == "http":
        return HttpWbClient(
            tokens=tokens,
            base_url=base_url,
            policy=policy,
            stocks_source=stocks_source,
        )
    return FixtureWbClient(fixtures_dir)


def policy_from(settings) -> RetryPolicy:
    """Политика повторов и паузы из настроек приложения (задача E-5)."""
    return RetryPolicy(
        attempts=settings.wb_max_attempts,
        backoff_seconds=settings.wb_retry_backoff_seconds,
        max_wait_seconds=settings.wb_max_wait_seconds,
        min_interval_seconds=settings.wb_min_interval_seconds,
        max_pages=settings.wb_max_pages,
        status_interval_seconds=settings.wb_status_interval_seconds,
        report_wait_seconds=settings.wb_report_wait_seconds,
    )
