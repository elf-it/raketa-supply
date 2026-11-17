"""Общее хранилище посчитанных рядов (задача G-14).

Кэш расчёта живёт в памяти процесса: у каждого воркера свой, и после
перезапуска первый запрос считает всё заново (`services/calc_cache.py`).
Неверных чисел это не даёт — годность записи решают номера версий из базы, —
но на большом каталоге это N копий одного и того же и N холодных стартов.

Здесь второй уровень: одно хранилище на все процессы. Включается настройкой
``CALC_CACHE_URL``; пусто — всё работает ровно как раньше, без лишней зависимости
и без лишнего сетевого похода.

Решения и почему так:

* **Номера версий — в ключе, а не в значении.** Запись, посчитанная по старым
  данным, не может быть выдана: её ключ больше никто не спросит. Поэтому
  хранилищу не нужны ни инвалидация, ни согласование между воркерами — только
  срок жизни, чтобы ненужные ключи уходили сами.

* **Чтение пачкой.** Экран матрицы трогает тысячу пар; тысяча отдельных походов
  в Redis стоила бы дороже самого расчёта. Ряды читаются одним ``MGET``
  и пишутся одним конвейером.

* **JSON, а не pickle.** Значения приезжают из сети, и ``pickle`` на таком входе
  — это выполнение кода при повреждённом или подменённом хранилище. JSON
  медленнее, но цена известна и она в разы меньше пересчёта.

* **Падение хранилища ничего не ломает.** Любая ошибка — это промах кэша,
  а не ошибка запроса. После ошибки поход в хранилище пропускается на минуту:
  иначе недоступный Redis добавлял бы таймаут к каждому запросу — то есть
  делал бы ровно то, ради чего кэш ставили, только наоборот.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Protocol

from ..calc import SeriesResult, Status, WeekCell
from ..observability import event

#: Сколько секунд не ходить в хранилище после ошибки.
BREAKER_SECONDS = 60


def encode(result: SeriesResult) -> str:
    """Ряд в компактный JSON: ячейка — список значений в фиксированном порядке."""
    return json.dumps(
        {
            "s": result.status.value,
            "c": result.coverage_weeks,
            "o": result.opening_stock,
            "w": [
                [
                    cell.week,
                    cell.is_closed,
                    cell.original_forecast,
                    cell.adapted_forecast,
                    cell.real_stock,
                    cell.adapted_stock,
                    cell.fact_sales,
                    cell.supply,
                    cell.in_transit,
                    cell.diff_percent,
                    cell.shortage_units,
                    cell.stock_limited,
                    cell.is_manual,
                ]
                for cell in result.cells
            ],
        },
        separators=(",", ":"),
    )


def decode(raw: str | bytes) -> SeriesResult:
    """Обратное преобразование. Любая несовместимость — это ValueError выше."""
    data = json.loads(raw)
    cells = [
        WeekCell(
            week=item[0],
            is_closed=item[1],
            original_forecast=item[2],
            adapted_forecast=item[3],
            real_stock=item[4],
            adapted_stock=item[5],
            fact_sales=item[6],
            supply=item[7],
            in_transit=item[8],
            diff_percent=item[9],
            shortage_units=item[10],
            stock_limited=item[11],
            is_manual=item[12],
        )
        for item in data["w"]
    ]
    return SeriesResult(
        cells=cells,
        status=Status(data["s"]),
        coverage_weeks=data["c"],
        opening_stock=data["o"],
    )


class SeriesStore(Protocol):
    """Что кэш ждёт от общего хранилища."""

    def get_many(self, keys: list[str]) -> dict[str, SeriesResult]: ...

    def put_many(self, items: dict[str, SeriesResult]) -> None: ...

    def describe(self) -> str: ...


class RedisSeriesStore:
    """Хранилище поверх Redis. Ошибки гасятся: кэш не может ломать расчёт."""

    def __init__(self, url: str, ttl_seconds: int = 3600, prefix: str = "raketa:series") -> None:
        self.url = url
        self.ttl_seconds = ttl_seconds
        self.prefix = prefix
        self.hits = 0
        self.misses = 0
        self.errors = 0
        self._client = None
        self._blocked_until = 0.0

    # --------------------------------------------------------------- соединение

    def _connect(self):
        if self._client is not None:
            return self._client
        # Импорт по месту: без настройки зависимость не нужна вовсе.
        import redis

        self._client = redis.Redis.from_url(
            self.url, socket_timeout=1.0, socket_connect_timeout=1.0
        )
        return self._client

    def _available(self) -> bool:
        return time.monotonic() >= self._blocked_until

    def _failed(self, error: Exception) -> None:
        """Ошибка хранилища: считаем, молчим минуту, работаем без него."""
        self.errors += 1
        self._client = None
        was_open = self._blocked_until
        self._blocked_until = time.monotonic() + BREAKER_SECONDS
        if was_open <= time.monotonic():
            # Строка в лог один раз на окно, а не на каждый запрос.
            event(
                "общий кэш расчёта недоступен",
                level=logging.WARNING,
                error=f"{type(error).__name__}: {error}",
                blocked_seconds=BREAKER_SECONDS,
            )

    # ------------------------------------------------------------------ чтение

    def key(self, raw: str) -> str:
        return f"{self.prefix}:{raw}"

    def get_many(self, keys: list[str]) -> dict[str, SeriesResult]:
        if not keys or not self._available():
            return {}
        try:
            client = self._connect()
            values = client.mget([self.key(key) for key in keys])
        except Exception as error:  # noqa: BLE001 — любая ошибка это промах
            self._failed(error)
            return {}

        found: dict[str, SeriesResult] = {}
        for key, raw in zip(keys, values, strict=True):
            if raw is None:
                continue
            try:
                found[key] = decode(raw)
            except Exception:  # noqa: BLE001 — испорченное значение это промах
                self.errors += 1
        self.hits += len(found)
        self.misses += len(keys) - len(found)
        return found

    def put_many(self, items: dict[str, SeriesResult]) -> None:
        if not items or not self._available():
            return
        try:
            client = self._connect()
            pipe = client.pipeline(transaction=False)
            for key, result in items.items():
                pipe.set(self.key(key), encode(result), ex=self.ttl_seconds)
            pipe.execute()
        except Exception as error:  # noqa: BLE001
            self._failed(error)

    def describe(self) -> str:
        """Адрес без пароля: строку видно в ответе ручки состояния."""
        safe = self.url
        if "@" in safe:
            scheme, _, tail = safe.partition("://")
            safe = f"{scheme}://***@{tail.rpartition('@')[2]}"
        return safe


def make_store(url: str, ttl_seconds: int) -> SeriesStore | None:
    """Хранилище по настройке. Пусто — общего кэша нет, и это нормальный режим."""
    if not url.strip():
        return None
    return RedisSeriesStore(url.strip(), ttl_seconds=ttl_seconds)
