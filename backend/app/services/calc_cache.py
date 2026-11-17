"""Кэш недельных рядов по парам (магазин, товар) — задача G-13.

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

Второй уровень — общий (задача G-14). По умолчанию кэш живёт только в памяти
процесса: у каждого воркера свой, и после перезапуска первый запрос считает всё
заново. Настройка ``CALC_CACHE_URL`` добавляет общее хранилище (Redis), в которое
смотрят все процессы: номера версий входят в ключ, поэтому запись, посчитанная
по старым данным, просто не находится — согласовывать между воркерами нечего.
Память процесса остаётся первым уровнем: ходить по сети за каждой из тысячи пар
дороже, чем посчитать их заново, а вот один поход за всей пачкой — дешевле.
Подробности и деградация — `services/series_store.py`.

Что кэш не умеет. Подмена базы под работающим процессом (восстановление из копии,
`app.seed --reset`) обнуляет номера версий, и записи в памяти станут выглядеть
годными: после такого процесс нужно перезапустить, а общее хранилище — очистить.
Сид свой кэш чистит сам. Замеры и пределы — `docs/08-performance.md`.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Sequence
from functools import lru_cache

from ..calc import CalcSettings, SeriesResult
from ..config import get_settings
from ..observability import event
from .series_store import SeriesStore, make_store

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


def _warn_cache_is_too_small(max_entries: int, evictions: int) -> None:
    """Говорит, что кэш расчёта не вмещает каталог (найдено замером, G-6).

    На 10 800 парах с пределом по умолчанию (10 000 записей) кэш вытесняет
    то, что сам же положил: экран отвечает 4,3 с вместо 1,1 с — медленнее,
    чем вовсе без кэша, потому что к расчёту добавляется запись. Снаружи
    это выглядит просто как «система тормозит», и найти причину нечем.
    """
    event(
        "кэш расчёта не вмещает каталог",
        level=logging.WARNING,
        max_entries=max_entries,
        evictions=evictions,
        advice=(
            "поднимите CALC_CACHE_ENTRIES: нужно не меньше, "
            "чем позиций × число экранов (матрица и закупка — два)"
        ),
    )


class SeriesCache:
    """Записи «пара + подпись → недельный ряд» с ограничением по числу.

    Предел нужен, чтобы кэш не съел память на большом каталоге: 1080 пар на двух
    осях — это 2160 записей, а на реальных объёмах (вопрос Q-9) число пар
    неизвестно. При переполнении вытесняется самая давно не нужная запись.

    Класс потокобезопасен: синхронные ручки FastAPI выполняются в пуле потоков,
    то есть несколько запросов читают и пишут кэш одновременно.
    """

    def __init__(self, max_entries: int, shared: SeriesStore | None = None) -> None:
        self.max_entries = max(0, max_entries)
        #: ключ → (номер версии пары, общий номер, результат)
        self._entries: OrderedDict[CacheKey, tuple[int, int, SeriesResult]] = OrderedDict()
        self._lock = threading.Lock()
        #: Второй уровень: одно хранилище на все процессы, если настроено.
        self.shared = shared
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.shared_hits = 0
        #: Сколько раз общее хранилище не сработало. Ноль — оно в порядке.
        self.shared_failures = 0
        #: О переполнении говорится один раз на процесс: это настройка,
        #: а не событие, и повторять её каждый запрос — значит засорить лог.
        self._warned_full = False

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
            warn = self.evictions > self.max_entries and not self._warned_full
            if warn:
                self._warned_full = True
        if warn:
            # Вытеснено больше, чем кэш вмещает: значит, запрос выбрасывает
            # то, что сам же положил, и попаданий не будет вовсе. Это не просто
            # бесполезный кэш — за запись платят каждым запросом. Молчать об этом
            # нельзя: снаружи это выглядит как «система почему-то медленная».
            _warn_cache_is_too_small(self.max_entries, self.evictions)

    # ----------------------------------------------------------- работа пачкой

    def get_many(self, wanted: list[tuple[int, str, int, int]]) -> dict[int, SeriesResult]:
        """Готовые ряды для пачки пар: сначала память, потом общее хранилище.

        ``wanted`` — список ``(пара, подпись, номер версии пары, общий номер)``.
        Общее хранилище спрашивается один раз на всю пачку: тысяча отдельных
        походов по сети стоила бы дороже, чем посчитать эти ряды заново.
        """
        found: dict[int, SeriesResult] = {}
        missing: list[tuple[int, str, int, int]] = []
        for pair_id, signature, revision, epoch in wanted:
            ready = self.get(pair_id, signature, revision, epoch)
            if ready is None:
                missing.append((pair_id, signature, revision, epoch))
            else:
                found[pair_id] = ready

        if not missing or self.shared is None or not self.enabled:
            return found

        keys = {shared_key(*item): item for item in missing}
        for key, result in self._ask_shared(list(keys)).items():
            pair_id, signature, revision, epoch = keys[key]
            # Запись из общего хранилища кладётся и в память: следующий запрос
            # этого процесса уже не пойдёт по сети.
            self.put(pair_id, signature, revision, epoch, result)
            found[pair_id] = result
            with self._lock:
                self.shared_hits += 1
                # Промах памяти уже посчитан в get(): из общего хранилища это
                # попадание, и считать его промахом было бы неправдой.
                self.misses -= 1
        return found

    def put_many(self, items: list[tuple[int, str, int, int, SeriesResult]]) -> None:
        """Запоминает посчитанные ряды в памяти и в общем хранилище."""
        for pair_id, signature, revision, epoch, result in items:
            self.put(pair_id, signature, revision, epoch, result)
        if self.shared is not None and self.enabled and items:
            try:
                self.shared.put_many(
                    {
                        shared_key(pair_id, signature, revision, epoch): result
                        for pair_id, signature, revision, epoch, result in items
                    }
                )
            except Exception:  # noqa: BLE001 — см. _ask_shared
                with self._lock:
                    self.shared_failures += 1

    def _ask_shared(self, keys: list[str]) -> dict[str, SeriesResult]:
        """Общее хранилище за щитом: кэш не может ломать расчёт.

        Реализация хранилища гасит свои ошибки сама, но гарантия должна быть
        здесь: любая неожиданность на стороне кэша — это промах, а не ошибка
        запроса. Числа посчитаются заново, человек ничего не заметит, а счётчик
        в `/stats/calc-cache` покажет, что с хранилищем что-то не так.
        """
        assert self.shared is not None
        try:
            return self.shared.get_many(keys)
        except Exception:  # noqa: BLE001 — любая ошибка кэша это промах
            with self._lock:
                self.shared_failures += 1
            return {}

    def clear(self) -> None:
        """Выбрасывает всё. Нужна после подмены базы и в тестах."""
        with self._lock:
            self._entries.clear()

    def reset_stats(self) -> None:
        with self._lock:
            self.hits = self.misses = self.evictions = 0
            self.shared_hits = self.shared_failures = 0

    def stats(self) -> dict:
        with self._lock:
            out = {
                "entries": len(self._entries),
                "max_entries": self.max_entries,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
                "shared_hits": self.shared_hits,
                "shared_failures": self.shared_failures,
                "shared": None,
            }
        if self.shared is not None:
            out["shared"] = {
                "where": self.shared.describe(),
                "hits": getattr(self.shared, "hits", 0),
                "misses": getattr(self.shared, "misses", 0),
                "errors": getattr(self.shared, "errors", 0),
            }
        return out


def shared_key(pair_id: int, signature: str, revision: int, epoch: int) -> str:
    """Ключ общего хранилища: номера версий в ключе, а не в значении.

    Поэтому запись, посчитанную по старым данным, невозможно получить — её ключ
    больше никто не спросит, и инвалидация хранилищу не нужна вовсе.
    """
    return f"{pair_id}:{signature}:{revision}:{epoch}"


@lru_cache
def get_series_cache() -> SeriesCache:
    """Кэш процесса. Один на приложение — как и счётчик попыток входа."""
    settings = get_settings()
    return SeriesCache(
        settings.calc_cache_entries,
        shared=make_store(settings.calc_cache_url, settings.calc_cache_ttl_seconds),
    )
