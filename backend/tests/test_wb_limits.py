"""Лимиты, повторы и контроль дублей в загрузке WB (задача E-5).

Боевых ключей нет (вопрос Q-3), поэтому путь проверяется подставным
транспортом: он отвечает теми же кодами и заголовками, что описаны
в документации WB, — 429 с ``X-Ratelimit-Retry``, 500, 401, страницы продаж
с курсором ``lastChangeDate``. Часы и сон тоже подставные: тест про минутную
паузу не должен идти минуту.

Самое дорогое здесь — не скорость, а две тихие ошибки: сложить одну продажу
дважды (завышенный спрос → лишний заказ) и загрузить данные не полностью,
сделав вид, что всё хорошо (заниженный спрос → недозаказ).
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.integrations.wb import HttpWbClient, RetryPolicy, Traffic, WbApiError, sync_store
from app.integrations.wb.limits import (
    REMAINING_HEADER,
    RESET_HEADER,
    RETRY_HEADER,
    Dedup,
    Throttle,
    remaining_from,
    should_retry,
    wait_for,
)
from app.models import Store, SyncRun, SyncStatus

FROM = date(2026, 1, 1)
POLICY = RetryPolicy(attempts=3, backoff_seconds=2.0, max_wait_seconds=120.0)


class Clock:
    """Часы, которые идут только когда спят."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_client(handler, policy: RetryPolicy = POLICY, clock: Clock | None = None):
    clock = clock or Clock()
    client = HttpWbClient(
        tokens={"store01": "token-1"},
        base_url="https://statistics-api.example",
        policy=policy,
        sleep=clock.sleep,
        clock=clock,
        transport=httpx.MockTransport(handler),
    )
    return client, clock


# ------------------------------------------------------------------ что повторять


def test_only_the_limit_and_service_errors_are_retried():
    """4XX повторять нельзя: по документации такой ответ стоит десяти запросов,
    и ни токен, ни параметры повтор не исправит."""
    assert should_retry(429)
    assert should_retry(500)
    assert should_retry(503)
    assert not should_retry(400)
    assert not should_retry(401)
    assert not should_retry(403)
    assert not should_retry(404)


def test_the_server_decides_how_long_to_wait():
    """Раньше названного сервером срока повтор всё равно вернёт 429."""
    assert wait_for({RETRY_HEADER: "7"}, POLICY, attempt=1) == 7
    assert wait_for({"Retry-After": "5"}, POLICY, attempt=1) == 5
    assert wait_for({RESET_HEADER: "29"}, POLICY, attempt=1) == 29
    # Свой заголовок главнее и стандартного, и срока восстановления всплеска.
    assert wait_for({RETRY_HEADER: "2", RESET_HEADER: "29"}, POLICY, attempt=1) == 2


def test_without_an_answer_the_pause_doubles():
    assert wait_for({}, POLICY, attempt=1) == 2
    assert wait_for({}, POLICY, attempt=2) == 4
    assert wait_for(None, POLICY, attempt=3) == 8


def test_a_long_wait_is_capped():
    """Ответ «подождите час» не должен держать прогон час и блокировку с ним."""
    assert wait_for({RETRY_HEADER: "3600"}, POLICY, attempt=1) == POLICY.max_wait_seconds
    assert POLICY.backoff(20) == POLICY.max_wait_seconds


def test_headers_are_read_whatever_their_case():
    """HTTP-клиент отдаёт заголовки в нижнем регистре, а документация пишет их
    как ``X-Ratelimit-Retry``. Поиск по точному написанию молча не нашёл бы
    названный сервером срок, и пауза считалась бы дважды."""
    assert wait_for({"x-ratelimit-retry": "9"}, POLICY, attempt=1) == 9
    assert wait_for({"retry-after": "4"}, POLICY, attempt=1) == 4
    assert remaining_from({"x-ratelimit-remaining": "2"}) == 2


def test_a_broken_header_is_ignored():
    assert wait_for({RETRY_HEADER: "скоро"}, POLICY, attempt=1) == 2
    assert wait_for({RETRY_HEADER: "0"}, POLICY, attempt=1) == 2
    assert remaining_from({REMAINING_HEADER: "нет"}) is None
    assert remaining_from({}) is None
    assert remaining_from({REMAINING_HEADER: "3"}) == 3


# ----------------------------------------------------------------------- паузы


def test_the_pause_is_kept_per_cabinet_and_method():
    """Лимит WB задан на профиль продавца и метод: два кабинета — два лимита."""
    clock = Clock()
    throttle = Throttle(60.0, clock=clock, sleep=clock.sleep)

    throttle.after(("store01", "/sales"))
    assert throttle.before(("store02", "/sales")) == 0, "другой кабинет ждать не должен"
    assert throttle.before(("store01", "/stocks")) == 0, "другой метод — другой лимит"
    assert throttle.before(("store01", "/sales")) == 60


def test_a_burst_left_means_no_pause():
    """X-Ratelimit-Remaining больше нуля — сервер разрешил не ждать."""
    clock = Clock()
    throttle = Throttle(60.0, clock=clock, sleep=clock.sleep)

    throttle.after(("store01", "/sales"), {REMAINING_HEADER: "4"})

    assert throttle.before(("store01", "/sales")) == 0
    assert clock.slept == []


def test_after_the_limit_the_burst_is_forgotten():
    clock = Clock()
    throttle = Throttle(60.0, clock=clock, sleep=clock.sleep)
    throttle.after(("store01", "/sales"), {REMAINING_HEADER: "4"})

    throttle.forget(("store01", "/sales"))

    assert throttle.before(("store01", "/sales")) == 60


def test_time_already_passed_counts_towards_the_pause():
    clock = Clock()
    throttle = Throttle(60.0, clock=clock, sleep=clock.sleep)
    throttle.after(("store01", "/sales"))
    clock.now += 50

    assert throttle.before(("store01", "/sales")) == pytest.approx(10)


# --------------------------------------------------------------- контроль дублей


def test_a_row_seen_twice_is_dropped():
    dedup = Dedup()

    assert dedup.is_new("srid:a")
    assert not dedup.is_new("srid:a")
    assert dedup.is_new("srid:b")
    assert dedup.duplicates == 1


def test_a_row_without_an_identifier_passes():
    """Повтор такой строки узнать нечем, и отбрасывать её наугад нельзя."""
    dedup = Dedup()

    assert dedup.is_new(None)
    assert dedup.is_new(None)
    assert dedup.duplicates == 0


# ------------------------------------------------------------- запросы и повторы


def _sale(srid: str, changed: str, day: str = "2026-01-05T10:00:00", nm: int = 101) -> dict:
    return {"srid": srid, "lastChangeDate": changed, "date": day, "nmId": nm, "saleID": "S1"}


def test_the_limit_answer_is_retried_after_the_named_delay():
    answers = [
        httpx.Response(429, headers={RETRY_HEADER: "2"}, json={"detail": "too many"}),
        httpx.Response(200, json=[]),
    ]
    client, clock = make_client(lambda request: answers.pop(0))

    assert client.fetch_sales("store01", FROM) == []
    traffic = client.take_traffic()
    assert traffic.requests == 2
    assert traffic.retries == 1
    assert clock.slept == [2], "сервер назвал срок — своей минуты сверху быть не должно"
    assert traffic.waited_seconds == 2


def test_a_bad_token_is_not_retried():
    seen: list[str] = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(401, json={"title": "unauthorized"})

    client, clock = make_client(handler)

    with pytest.raises(WbApiError) as error:
        client.fetch_sales("store01", FROM)

    assert error.value.status == 401
    assert "unauthorized" in str(error.value)
    assert len(seen) == 1, "повтор 4XX тратит лимит в десять раз быстрее и не помогает"
    assert clock.slept == []


def test_a_service_error_runs_out_of_attempts():
    client, clock = make_client(lambda request: httpx.Response(500, text="oops"))

    with pytest.raises(WbApiError) as error:
        client.fetch_sales("store01", FROM)

    assert error.value.status == 500
    assert "попыток: 3" in str(error.value)
    assert client.take_traffic().requests == 3
    # 5XX о лимите не говорит ничего, поэтому отступ свой (2, 4), а сверху
    # остаётся документированная пауза метода: между запросами — минута.
    assert clock.slept == [2, 58, 4, 56]


def test_a_network_failure_is_retried():
    attempts: list[int] = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("сеть недоступна")
        return httpx.Response(200, json=[])

    client, clock = make_client(handler)

    assert client.fetch_sales("store01", FROM) == []
    assert client.take_traffic().retries == 2
    assert clock.slept == [2, 58, 4, 56], "сеть ничего не сказала — пауза метода остаётся"


def test_a_network_failure_runs_out_of_attempts():
    def handler(request):
        raise httpx.ConnectTimeout("нет ответа")

    client, _ = make_client(handler)

    with pytest.raises(WbApiError) as error:
        client.fetch_sales("store01", FROM)

    assert "ConnectTimeout" in str(error.value)
    assert "попыток: 3" in str(error.value)


def test_an_answer_that_is_not_json_is_an_error_not_a_crash():
    client, _ = make_client(lambda request: httpx.Response(200, text="<html>проверка браузера"))

    with pytest.raises(WbApiError) as error:
        client.fetch_sales("store01", FROM)

    assert "JSON" in str(error.value)


def test_the_token_goes_in_the_header_and_nowhere_else():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=[])

    client, _ = make_client(handler)
    client.fetch_sales("store01", FROM)

    assert seen[0].headers["authorization"] == "token-1"
    assert "token-1" not in str(seen[0].url), "ключ в строке запроса утёк бы в логи прокси"


def test_a_cabinet_without_a_token_says_so():
    client, _ = make_client(lambda request: httpx.Response(200, json=[]))

    with pytest.raises(RuntimeError, match="WB_TOKENS"):
        client.fetch_sales("store-unknown", FROM)


# -------------------------------------------------------------------- страницы


def test_sales_are_read_page_by_page():
    pages = [
        [_sale("a", "2026-01-05T10:00:00"), _sale("b", "2026-01-06T11:00:00")],
        [_sale("c", "2026-01-07T12:00:00")],
        [],
    ]
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.params["dateFrom"])
        return httpx.Response(200, json=pages[len(asked) - 1])

    client, _ = make_client(handler, policy=RetryPolicy(attempts=2, min_interval_seconds=0))

    rows = client.fetch_sales("store01", FROM)
    traffic = client.take_traffic()

    assert len(rows) == 3
    assert traffic.pages == 3
    assert not traffic.truncated, "пустой массив означает, что всё выгружено"
    # Курсор — lastChangeDate последней строки предыдущего ответа.
    assert asked == ["2026-01-01", "2026-01-06T11:00:00", "2026-01-07T12:00:00"]


def test_the_cursor_is_passed_through_untouched():
    """Время у WB московское и с миллисекундами: любое приведение типа сдвинет
    курсор и даст сутки чужих строк или потерю своих."""
    cursor = "2026-01-06T11:00:00.123456"
    pages = [[_sale("a", cursor)], []]
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.params["dateFrom"])
        return httpx.Response(200, json=pages[len(asked) - 1])

    client, _ = make_client(handler, policy=RetryPolicy(attempts=2, min_interval_seconds=0))
    client.fetch_sales("store01", FROM)

    assert asked[1] == cursor


def test_the_boundary_row_is_not_counted_twice():
    """Курсор включает последнюю строку предыдущей страницы.

    Строка продажи — это единица товара. Сложенная дважды, она завышает спрос,
    а по спросу считается закупка: ошибка выглядит правдоподобно и уедет
    поставщику.
    """
    first = [_sale("a", "2026-01-05T10:00:00"), _sale("b", "2026-01-06T11:00:00")]
    second = [_sale("b", "2026-01-06T11:00:00"), _sale("c", "2026-01-07T12:00:00")]
    pages = [first, second, []]
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.params["dateFrom"])
        return httpx.Response(200, json=pages[len(asked) - 1])

    client, _ = make_client(handler, policy=RetryPolicy(attempts=2, min_interval_seconds=0))

    rows = client.fetch_sales("store01", FROM)
    traffic = client.take_traffic()

    assert len(rows) == 3, "три разные продажи, а не четыре"
    assert traffic.duplicates == 1


def test_a_stuck_cursor_does_not_loop_forever():
    """Сервер отдаёт одно и то же: цикл обязан кончиться сам."""
    repeated = [_sale("a", "2026-01-05T10:00:00")]
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.params["dateFrom"])
        return httpx.Response(200, json=repeated)

    client, _ = make_client(
        handler, policy=RetryPolicy(attempts=2, min_interval_seconds=0, max_pages=50)
    )

    rows = client.fetch_sales("store01", FROM)

    assert len(rows) == 1
    assert len(asked) == 2, "вторая страница целиком повторная — дальше идти некуда"


def test_the_page_limit_marks_the_load_incomplete():
    """Предел страниц — не «и так хватит», а «данные неполные»."""
    counter = {"n": 0}

    def handler(request):
        counter["n"] += 1
        # Курсор каждый раз новый: данные не кончаются, кончаются страницы.
        changed = f"2026-02-{counter['n']:02d}T10:00:00"
        return httpx.Response(200, json=[_sale(f"s{counter['n']}", changed)])

    client, _ = make_client(
        handler, policy=RetryPolicy(attempts=2, min_interval_seconds=0, max_pages=3)
    )

    rows = client.fetch_sales("store01", FROM)
    traffic = client.take_traffic()

    assert len(rows) == 3
    assert traffic.pages == 3
    assert traffic.truncated


def test_the_minute_between_pages_is_waited_out():
    """У метода «Продажи» лимит — 1 запрос в минуту на профиль продавца."""
    pages = [[_sale("a", "2026-01-05T10:00:00")], []]
    asked: list[str] = []

    def handler(request):
        asked.append(request.url.params["dateFrom"])
        return httpx.Response(200, json=pages[len(asked) - 1])

    client, clock = make_client(handler, policy=RetryPolicy(attempts=2, min_interval_seconds=60.0))
    client.fetch_sales("store01", FROM)

    assert clock.slept == [60]
    assert client.take_traffic().waited_seconds == 60


def test_stocks_are_a_single_snapshot():
    asked: list[str] = []

    def handler(request):
        asked.append(str(request.url.path))
        return httpx.Response(
            200,
            json=[{"nmId": 101, "lastChangeDate": "2026-01-05T10:00:00", "quantity": 7}],
        )

    client, _ = make_client(handler)
    rows = client.fetch_stocks("store01", FROM)

    assert [row.quantity for row in rows] == [7.0]
    assert asked == ["/api/v1/supplier/stocks"], "остатки читаются одним запросом"


# ------------------------------------------------------- это видно в журнале


class StubClient:
    """Клиент, который ничего не грузит, но рассказывает, чего это стоило."""

    def __init__(self, traffic: Traffic) -> None:
        self._traffic = traffic

    def fetch_cards(self, store_code: str):
        return []

    def fetch_sales(self, store_code: str, date_from: date):
        return []

    def fetch_stocks(self, store_code: str, date_from: date):
        return []

    def take_traffic(self) -> Traffic:
        return self._traffic


def _run_sync(traffic: Traffic):
    with SessionLocal() as session:
        store = session.scalars(select(Store).order_by(Store.code)).first()
        report = sync_store(session, store, StubClient(traffic), FROM)
        run = session.scalars(
            select(SyncRun).where(SyncRun.store_id == store.id).order_by(SyncRun.id.desc())
        ).first()
        return report, run.status, run.message, run.id


def _forget(run_id: int) -> None:
    with SessionLocal() as session:
        session.delete(session.get(SyncRun, run_id))
        session.commit()


def test_the_journal_remembers_what_the_load_cost(client):
    traffic = Traffic(requests=5, retries=2, pages=3, duplicates=12, waited_seconds=64.0)

    report, status, message, run_id = _run_sync(traffic)
    try:
        assert report.ok
        assert status is SyncStatus.SUCCESS
        assert "страниц 3" in message
        assert "повторных строк 12" in message
        assert "повторов запроса 2" in message
        assert "ожидание лимита 64 с" in message
    finally:
        _forget(run_id)


def test_an_incomplete_load_is_a_failed_run(client):
    """Неполные данные не должны выглядеть полными: по ним посчитается
    заниженный спрос и недозаказ, а числа на экране будут правдоподобны."""
    report, status, message, run_id = _run_sync(Traffic(requests=3, pages=3, truncated=True))
    try:
        assert not report.ok
        assert status is SyncStatus.FAILED
        assert "не полностью" in message
        assert "WB_MAX_PAGES" in message
    finally:
        _forget(run_id)


def test_a_quiet_load_says_nothing_extra(client):
    report, status, message, run_id = _run_sync(Traffic(requests=2, pages=1))
    try:
        assert report.ok
        assert status is SyncStatus.SUCCESS
        assert message is None, "про обычный прогон в журнале писать нечего"
    finally:
        _forget(run_id)


def test_the_fixture_client_needs_no_counters(client):
    """Фикстурный клиент счётчиков не ведёт, и загрузка от этого не ломается."""
    from app.integrations.wb import FixtureWbClient

    with SessionLocal() as session:
        store = session.scalars(select(Store).order_by(Store.code)).first()
        report = sync_store(session, store, FixtureWbClient(), datetime.now().date())
        run_id = session.scalars(
            select(SyncRun.id).where(SyncRun.store_id == store.id).order_by(SyncRun.id.desc())
        ).first()

    try:
        assert report.traffic is None
        assert report.ok
    finally:
        _forget(run_id)
