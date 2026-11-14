"""Кэш недельных рядов по парам (магазин, товар) — задача G-6.

Зачем. Расчёт идёт по всей выборке на каждый запрос: на 1080 парах это две трети
времени ответа, и дальше линейно. Постраничная выдача этого не лечит — окно строк
режет передачу, а не расчёт. При этом данные между запросами почти всегда те же:
человек листает таблицу, переключает уровень, уходит на закупку и возвращается.

Единица кэша — пара, а не экран. Пара считается независимо от остальных (на этом
же стоит запрет двойного учёта остатков), поэтому один набор записей обслуживает
все три уровня просмотра, любые фильтры и любую страницу: уровни — это разные
группировки одного множества рядов. Кэш по готовому экрану пришлось бы выбрасывать
целиком на каждую правку ячейки, то есть ровно в том режиме, в котором с системой
и работают.

Почему запись не портится. Годность записи решают номера версий из базы:
``store_products.calc_revision`` у пары и ``app_settings.calc_epoch`` на всё общее
(`app/revisions.py`). Память процесса здесь только хранилище: правка, прошедшая
через соседний воркер, поднимет номер в базе, и запись перестанет подходить сама.
Этим кэш отличается от счётчика попыток входа (G-12), где «у каждого процесса свой»
означает ослабленное ограничение: здесь неверный ответ исключён, а не смягчён.

Порядок чтения — часть договора. Номера версий читаются раньше данных: они
приходят вместе со списком пар, и только потом грузится всё остальное. Если между
этими двумя чтениями кто-то успел закоммитить правку, запись окажется помечена
устаревшим номером — её никто больше не спросит. Обратный порядок дал бы свежий
номер на старых данных, то есть молча неверные числа; пересчёт впустую — цена
за то, что такой случай невозможен.

Что кэш не умеет. Он не переживает перезапуск процесса (первый запрос после
развёртывания считает всё заново) и у каждого воркера свой — память умножается
на их число, прогреваются они независимо. Подмена базы под работающим процессом
(восстановление из копии, `app.seed --reset`) обнуляет номера версий, и записи
в памяти станут выглядеть годными: после такого процесс нужно перезапустить.
Сид свой кэш чистит сам. Замеры и пределы — `docs/08-performance.md`.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Sequence
from functools import lru_cache

from ..calc import CalcSettings, SeriesResult
from ..config import get_settings

#: Ключ записи: пара и подпись оси недель вместе с настройками расчёта.
CacheKey = tuple[int, str]


def series_signature(weeks: Sequence[str], last_closed: str | None, settings: CalcSettings) -> str:
    """Подпись условий расчёта: та же пара с другой осью недель — другая запись.

    В подпись входит всё, что передаётся в расчёт помимо данных самой пары:
    список недель, последняя закрытая неделя и пороги. Экран матрицы и экран
    закупки берут разные оси (матрица — с историей, закупка — на весь горизонт),
    и их результаты не должны подменять друг друга.
    """
    raw = "\n".join(
        [
            ",".join(weeks),
            last_closed or "",
            repr(
                (
                    settings.over_coverage_weeks,
                    settings.short_horizon_weeks,
                    settings.demand_avg_weeks,
                    settings.horizon_weeks,
                )
            ),
        ]
    )
    return hashlib.blake2s(raw.encode("utf-8"), digest_size=16).hexdigest()


class SeriesCache:
    """Записи «пара + подпись → недельный ряд» с ограничением по числу.

    Предел нужен, чтобы кэш не съел память на большом каталоге: 1080 пар на двух
    осях — это 2160 записей, а на реальных объёмах (вопрос Q-9) число пар
    неизвестно. При переполнении вытесняется самая давно не нужная запись.

    Класс потокобезопасен: синхронные ручки FastAPI выполняются в пуле потоков,
    то есть несколько запросов читают и пишут кэш одновременно.
    """

    def __init__(self, max_entries: int) -> None:
        self.max_entries = max(0, max_entries)
        #: ключ → (номер версии пары, общий номер, результат)
        self._entries: OrderedDict[CacheKey, tuple[int, int, SeriesResult]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    @property
    def enabled(self) -> bool:
        return self.max_entries > 0

    def get(self, pair_id: int, signature: str, revision: int, epoch: int) -> SeriesResult | None:
        """Готовый ряд, если он посчитан по тем же версиям данных."""
        if not self.enabled:
            return None
        key = (pair_id, signature)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry[0] != revision or entry[1] != epoch:
                self.misses += 1
                # Устаревшая запись не удаляется: следующая запись по тому же
                # ключу её перезапишет, а лишнее удаление только путало бы учёт.
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[2]

    def put(
        self, pair_id: int, signature: str, revision: int, epoch: int, result: SeriesResult
    ) -> None:
        """Запоминает ряд. Результат после этого не правят на месте — он общий."""
        if not self.enabled:
            return
        key = (pair_id, signature)
        with self._lock:
            self._entries[key] = (revision, epoch, result)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def clear(self) -> None:
        """Выбрасывает всё. Нужна после подмены базы и в тестах."""
        with self._lock:
            self._entries.clear()

    def reset_stats(self) -> None:
        with self._lock:
            self.hits = self.misses = self.evictions = 0

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }


@lru_cache
def get_series_cache() -> SeriesCache:
    """Кэш процесса. Один на приложение — как и счётчик попыток входа."""
    return SeriesCache(get_settings().calc_cache_entries)
