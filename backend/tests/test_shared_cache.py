"""Общий кэш расчёта: второй уровень поверх памяти процесса (задача G-14).

Главные проверки — те, ради которых общий кэш вообще нужен и ради которых он
опасен. Первая: второй процесс пользуется тем, что посчитал первый, и получает
ровно те же числа. Вторая: запись, посчитанная по старым данным, из него
не достаётся — номера версий входят в ключ. Третья: упавшее хранилище
не ломает расчёт и не замедляет его надолго.

Настоящий Redis здесь не нужен: хранилище спрятано за интерфейсом, и подменный
словарь проверяет логику. Проверка живого клиента — в замерах
`docs/08-performance.md`.
"""

from __future__ import annotations

from app.calc import CalcSettings, Status, WeekCell
from app.calc.types import SeriesResult
from app.services.calc_cache import SeriesCache, series_signature, shared_key
from app.services.series_store import RedisSeriesStore, decode, encode

SIGNATURE = series_signature(["2026-W46", "2026-W47"], "2026-W45", CalcSettings())


def _result(value: float = 10.0) -> SeriesResult:
    return SeriesResult(
        cells=[
            WeekCell(
                week="2026-W46",
                is_closed=True,
                original_forecast=value,
                adapted_forecast=value - 1,
                real_stock=5.5,
                adapted_stock=5.5,
                fact_sales=None,
                supply=2.0,
                in_transit=1.0,
                diff_percent=None,
                shortage_units=0.0,
                stock_limited=True,
                is_manual=False,
            ),
            WeekCell(week="2026-W47", original_forecast=value + 1, fact_sales=3.0),
        ],
        status=Status.SHORT,
        coverage_weeks=2.5,
        opening_stock=7.0,
    )


class FakeStore:
    """Общее хранилище в словаре: тот же интерфейс, что у Redis-варианта."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.reads = 0
        self.writes = 0
        self.hits = 0
        self.misses = 0
        self.errors = 0

    def get_many(self, keys: list[str]) -> dict[str, SeriesResult]:
        self.reads += 1
        found = {key: decode(self.data[key]) for key in keys if key in self.data}
        self.hits += len(found)
        self.misses += len(keys) - len(found)
        return found

    def put_many(self, items: dict[str, SeriesResult]) -> None:
        self.writes += 1
        for key, result in items.items():
            self.data[key] = encode(result)

    def describe(self) -> str:
        return "fake://store"


class BrokenStore(FakeStore):
    """Хранилище, которое всегда падает."""

    def get_many(self, keys: list[str]) -> dict[str, SeriesResult]:
        self.reads += 1
        raise RuntimeError("redis недоступен")

    def put_many(self, items: dict[str, SeriesResult]) -> None:
        self.writes += 1
        raise RuntimeError("redis недоступен")


# ------------------------------------------------------------------ сериализация


def test_series_survives_the_trip_through_the_store():
    """Через хранилище ряд должен вернуться тем же, иначе числа разойдутся."""
    original = _result()

    restored = decode(encode(original))

    assert restored == original
    assert restored.cells[0].fact_sales is None, "пустое значение не стало нулём"
    assert restored.cells[1].fact_sales == 3.0
    assert restored.status is Status.SHORT
    assert restored.by_week()["2026-W47"].original_forecast == 11.0


# ----------------------------------------------------------- два процесса, один кэш


def test_second_process_uses_what_the_first_one_computed():
    """Ради этого общий кэш и нужен: новый воркер не считает всё заново."""
    store = FakeStore()
    first = SeriesCache(100, shared=store)
    second = SeriesCache(100, shared=store)

    first.put_many([(1, SIGNATURE, 3, 7, _result(42.0))])
    found = second.get_many([(1, SIGNATURE, 3, 7)])

    assert found[1].cells[0].original_forecast == 42.0
    assert second.shared_hits == 1
    assert second.stats()["shared"]["hits"] == 1
    # И в память второго процесса запись легла: по сети он больше не пойдёт.
    assert second.get(1, SIGNATURE, 3, 7) is not None
    assert store.reads == 1, "одна пачка — один поход в хранилище"


def test_stale_entry_is_not_served_from_the_shared_store():
    """Номера версий в ключе: запись по старым данным никто не спросит."""
    store = FakeStore()
    cache = SeriesCache(100, shared=store)
    cache.put_many([(1, SIGNATURE, 3, 7, _result(42.0))])

    other = SeriesCache(100, shared=store)
    assert other.get_many([(1, SIGNATURE, 4, 7)]) == {}, "правка пары"
    assert other.get_many([(1, SIGNATURE, 3, 8)]) == {}, "общая правка"
    assert other.get_many([(1, SIGNATURE, 3, 7)]), "та же версия — та же запись"


def test_one_trip_for_the_whole_batch():
    """Тысяча походов по сети стоила бы дороже, чем посчитать заново."""
    store = FakeStore()
    cache = SeriesCache(1000, shared=store)
    cache.put_many([(pair, SIGNATURE, 1, 1, _result(float(pair))) for pair in range(50)])

    fresh = SeriesCache(1000, shared=store)
    found = fresh.get_many([(pair, SIGNATURE, 1, 1) for pair in range(50)])

    assert len(found) == 50
    assert store.reads == 1
    assert store.writes == 1


def test_local_memory_answers_before_the_network():
    store = FakeStore()
    cache = SeriesCache(100, shared=store)
    cache.put_many([(1, SIGNATURE, 1, 1, _result())])
    store.reads = 0

    cache.get_many([(1, SIGNATURE, 1, 1)])

    assert store.reads == 0, "в хранилище ходят только за тем, чего нет в памяти"


# ------------------------------------------------------------------- деградация


def test_broken_store_does_not_break_the_calculation():
    """Упавшее хранилище — это промах кэша, а не ошибка запроса.

    Гарантия стоит в кэше, а не в хранилище: падать может любая реализация,
    в том числе будущая, и расчёт не должен об этом знать.
    """
    cache = SeriesCache(100, shared=BrokenStore())

    cache.put_many([(1, SIGNATURE, 1, 1, _result())])
    found = cache.get_many([(2, SIGNATURE, 1, 1)])

    assert found == {}, "чего нет, того нет — и это нормальный ответ"
    # Запись в память прошла, несмотря на упавшее хранилище.
    assert cache.get(1, SIGNATURE, 1, 1) is not None
    assert cache.stats()["shared_failures"] == 2, "счётчик покажет, что хранилище сломано"


def test_dead_store_is_skipped_for_a_while():
    """Иначе недоступный Redis добавлял бы таймаут к каждому запросу."""
    store = RedisSeriesStore("redis://127.0.0.1:6399/0", ttl_seconds=60)

    assert store.get_many(["a"]) == {}
    first_attempt = store.errors
    assert first_attempt == 1

    store.get_many(["b"])
    store.put_many({"c": _result()})

    assert store.errors == 1, "после ошибки в хранилище не ходят целую минуту"
    assert store.describe() == "redis://127.0.0.1:6399/0"


def test_password_is_not_shown_in_the_address():
    """Адрес попадает в ответ ручки состояния: пароля там быть не должно."""
    store = RedisSeriesStore("redis://:secret@cache.local:6379/0", ttl_seconds=60)

    assert "secret" not in store.describe()
    assert store.describe() == "redis://***@cache.local:6379/0"


def test_cache_without_a_shared_store_works_as_before():
    cache = SeriesCache(10)

    cache.put_many([(1, SIGNATURE, 1, 1, _result())])

    assert cache.get_many([(1, SIGNATURE, 1, 1)])
    assert cache.stats()["shared"] is None


def test_key_names_the_versions():
    assert shared_key(7, "abc", 3, 9) == "7:abc:3:9"


# ------------------------------------------------------------------------ ручка


def test_stats_endpoint_tells_what_the_cache_is_doing(client):
    answer = client.get("/api/v1/stats/calc-cache")

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert {"entries", "max_entries", "hits", "misses", "evictions", "shared_hits"} <= set(body)
    assert body["max_entries"] > 0


def test_stats_endpoint_is_closed_to_strangers(anon_client):
    assert anon_client.get("/api/v1/stats/calc-cache").status_code == 401
