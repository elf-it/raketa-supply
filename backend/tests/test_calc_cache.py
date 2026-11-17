"""Кэш недельных рядов (задача G-13).

Главная проверка здесь одна и повторяется после каждого вида правки: ответ
с кэшем совпадает с ответом без кэша. Ускорение можно замерить, а вот ошибку
кэша замерить нечем — устаревшие числа выглядят как настоящие, и заказ по ним
уйдёт поставщику. Поэтому каждый тест сравнивает кэшированный ответ
с посчитанным заново.

Кэш между тестами намеренно не чистится: весь остальной набор тестов правит
данные и проверяет числа, то есть работает сторожем для инвалидации.
"""

from __future__ import annotations

from app.calc import CalcSettings, SeriesResult, WeekCell
from app.services.calc_cache import SeriesCache, get_series_cache, series_signature

MATRIX = "/api/v1/matrix?level=sku"
PURCHASE = "/api/v1/purchase?only_needed=false"
WEEKS = ["2026-W10", "2026-W11"]


def _result(value: float = 1.0) -> SeriesResult:
    return SeriesResult(cells=[WeekCell(week=WEEKS[0], original_forecast=value)])


def without_cache(client, url: str) -> dict:
    """Тот же запрос, но с выключенным кэшем — расчёт идёт по базе."""
    cache = get_series_cache()
    limit = cache.max_entries
    cache.max_entries = 0
    try:
        response = client.get(url)
        assert response.status_code == 200, response.text
        return response.json()
    finally:
        cache.max_entries = limit


def assert_matches_fresh_calculation(client, url: str = MATRIX) -> dict:
    cached = client.get(url)
    assert cached.status_code == 200, cached.text
    answer = cached.json()
    assert answer == without_cache(client, url), "кэш отдал не то, что даёт расчёт"
    return answer


# ------------------------------------------------------------------ сам кэш, без базы


def test_entry_serves_only_the_versions_it_was_built_for():
    cache = SeriesCache(max_entries=10)
    result = _result()
    cache.put(7, "sig", revision=3, epoch=2, result=result)

    assert cache.get(7, "sig", 3, 2) is result
    assert cache.get(7, "sig", 4, 2) is None, "данные пары изменились"
    assert cache.get(7, "sig", 3, 3) is None, "изменилось что-то общее для всех пар"
    assert cache.get(7, "other", 3, 2) is None, "другая ось недель — другой ряд"
    assert cache.get(8, "sig", 3, 2) is None, "запись принадлежит одной паре"


def test_signature_separates_what_changes_the_result():
    settings = CalcSettings()
    same = series_signature(WEEKS, "2026-W09", settings)

    assert series_signature(list(WEEKS), "2026-W09", settings) == same
    assert series_signature(WEEKS, "2026-W08", settings) != same
    assert series_signature([*WEEKS, "2026-W12"], "2026-W09", settings) != same
    assert series_signature(WEEKS, "2026-W09", CalcSettings(short_horizon_weeks=4)) != same


def test_limit_pushes_out_the_longest_unused():
    cache = SeriesCache(max_entries=2)
    cache.put(1, "sig", 1, 1, _result())
    cache.put(2, "sig", 1, 1, _result())
    # Обращение к первой записи делает её свежей, и вытеснена будет вторая.
    assert cache.get(1, "sig", 1, 1) is not None
    cache.put(3, "sig", 1, 1, _result())

    assert cache.get(2, "sig", 1, 1) is None
    assert cache.get(1, "sig", 1, 1) is not None
    assert cache.get(3, "sig", 1, 1) is not None
    assert cache.stats()["evictions"] == 1


def test_a_cache_smaller_than_the_catalogue_says_so_once(caplog):
    """Найдено замером (G-6): на 10 800 парах с пределом по умолчанию кэш
    вытесняет то, что сам же положил, и экран отвечает 4,3 с вместо 1,1 с —
    медленнее, чем вовсе без кэша. Снаружи это выглядит просто как «тормозит»,
    поэтому система обязана сказать об этом сама.
    """
    import logging

    cache = SeriesCache(max_entries=2)
    with caplog.at_level(logging.WARNING, logger="raketa"):
        # Шесть записей при пределе в две: вытеснено больше, чем кэш вмещает.
        for pair_id in range(1, 7):
            cache.put(pair_id, "sig", 1, 1, _result())

    warnings = [r for r in caplog.records if "не вмещает" in r.getMessage()]
    assert len(warnings) == 1, "об одной и той же настройке не кричат каждый запрос"
    assert "CALC_CACHE_ENTRIES" in str(getattr(warnings[0], "fields", ""))

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="raketa"):
        for pair_id in range(7, 12):
            cache.put(pair_id, "sig", 1, 1, _result())
    assert not [r for r in caplog.records if "не вмещает" in r.getMessage()]


def test_a_cache_that_fits_stays_silent(caplog):
    import logging

    cache = SeriesCache(max_entries=10)
    with caplog.at_level(logging.WARNING, logger="raketa"):
        for pair_id in range(1, 11):
            cache.put(pair_id, "sig", 1, 1, _result())

    assert not [r for r in caplog.records if "не вмещает" in r.getMessage()]


def test_zero_limit_turns_the_cache_off():
    cache = SeriesCache(max_entries=0)
    cache.put(1, "sig", 1, 1, _result())

    assert cache.enabled is False
    assert cache.get(1, "sig", 1, 1) is None
    assert cache.stats()["entries"] == 0


# ------------------------------------------------------------------- кэш в работе


def test_repeat_request_is_served_without_recalculation(client):
    first = assert_matches_fresh_calculation(client)
    cache = get_series_cache()
    cache.reset_stats()

    second = client.get(MATRIX).json()

    stats = cache.stats()
    assert second == first
    assert stats["misses"] == 0, "повторный запрос не должен считать заново"
    assert stats["hits"] == first["context"]["sku_count_total"]


def test_levels_and_pages_share_one_set_of_rows(client):
    """Уровни просмотра — разные группировки одних и тех же рядов."""
    client.get(MATRIX)
    cache = get_series_cache()
    cache.reset_stats()

    for url in (
        "/api/v1/matrix?level=store",
        "/api/v1/matrix?level=category",
        "/api/v1/matrix?level=sku&limit=5",
        "/api/v1/matrix?level=sku&limit=5&offset=5",
    ):
        assert client.get(url).status_code == 200

    assert cache.stats()["misses"] == 0


def test_edit_recalculates_only_the_edited_pair(client):
    before = client.get(MATRIX).json()
    pair_id = before["rows"][0]["meta"]["pair_id"]
    week = next(w["key"] for w in before["weeks"] if not w["is_closed"])
    cache = get_series_cache()
    cache.reset_stats()

    edit = client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 777})
    assert edit.status_code == 200, edit.text
    try:
        after = assert_matches_fresh_calculation(client)
        stats = cache.stats()

        assert after["rows"][0]["metrics"]["original_forecast"][week] == 777
        assert stats["misses"] == 1, "пересчитать нужно только правленую пару"
        assert stats["hits"] == before["context"]["sku_count_total"] - 1
    finally:
        client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})


def test_base_potential_change_recalculates_its_pair(client):
    before = client.get(MATRIX).json()
    row = before["rows"][0]
    pair_id = row["meta"]["pair_id"]
    potential = client.get(f"/api/v1/reference/base-potential?store_id={row['meta']['store_id']}")
    was = next(r for r in potential.json() if r["pair_id"] == pair_id)["base_potential"]
    cache = get_series_cache()
    cache.reset_stats()

    changed = client.put(
        f"/api/v1/reference/base-potential/{pair_id}", json={"base_potential": was + 25}
    )
    assert changed.status_code == 200, changed.text
    try:
        after = assert_matches_fresh_calculation(client)
        week = next(w["key"] for w in after["weeks"] if not w["is_closed"])

        assert cache.stats()["misses"] == 1
        assert (
            after["rows"][0]["metrics"]["original_forecast"][week]
            != before["rows"][0]["metrics"]["original_forecast"][week]
        )
    finally:
        client.put(f"/api/v1/reference/base-potential/{pair_id}", json={"base_potential": was})


def test_seasonality_change_sends_the_whole_selection_back_to_the_calculation(client):
    """Сезонность задаётся категории, и отметка на неё — общая.

    Отдельный номер версии у каждой пары означал бы обновление всей таблицы
    на правку одного коэффициента, поэтому кэш здесь сбрасывается целиком.
    """
    before = client.get(MATRIX).json()
    week = next(w for w in before["weeks"] if not w["is_closed"])
    category = client.get("/api/v1/reference/seasonality").json()[0]
    iso_week = week["iso_week"]
    was = category["coefficients"].get(str(iso_week), 1.0)
    cache = get_series_cache()
    cache.reset_stats()

    changed = client.put(
        f"/api/v1/reference/seasonality/{category['category_id']}",
        json={"coefficients": {iso_week: was + 0.5}},
    )
    assert changed.status_code == 200, changed.text
    try:
        assert_matches_fresh_calculation(client)

        assert cache.stats()["hits"] == 0
        assert cache.stats()["misses"] == before["context"]["sku_count_total"]
    finally:
        client.put(
            f"/api/v1/reference/seasonality/{category['category_id']}",
            json={"coefficients": {iso_week: was}},
        )


def test_cancelled_order_does_not_leave_a_stale_arrival(client):
    """Отмена убирает поставки запросом, минуя объекты сессии.

    Слушатель, который поднимает номера версий, таких изменений не видит, поэтому
    отмена отмечает пары сама. Если это забыть, кэш продолжит показывать приход
    по заказу, которого больше нет, — а потребность так и останется закрытой.
    """
    plan = client.get("/api/v1/purchase?only_needed=true").json()
    assert plan["rows"], "в демо-данных нет позиций к заказу"
    row = plan["rows"][0]
    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {"pair_id": row["pair_id"], "qty": row["qty"], "arrival_week": row["arrival_week"]}
            ]
        },
    )
    assert created.status_code == 201, created.text
    order_id = created.json()["id"]

    def set_status(status: str) -> None:
        response = client.post(f"/api/v1/orders/{order_id}/status", json={"status": status})
        assert response.status_code == 200, response.text

    set_status("placed")
    placed = assert_matches_fresh_calculation(client, PURCHASE)

    set_status("cancelled")
    cancelled = assert_matches_fresh_calculation(client, PURCHASE)

    def qty(answer: dict) -> int:
        return next(r for r in answer["rows"] if r["pair_id"] == row["pair_id"])["qty"]

    assert qty(placed) < row["qty"], "размещённый заказ должен был закрыть потребность"
    assert qty(cancelled) == row["qty"], "потребность должна вернуться после отмены"


def test_version_stamps_live_in_the_database(client):
    """Номер версии хранится в базе, а не в памяти процесса.

    Иначе правка, прошедшая через один воркер, не дошла бы до остальных: кэш
    у каждого свой, и узнать об изменении он может только из базы.
    """
    from app.db import SessionLocal
    from app.models import AppSettings, StoreProduct

    matrix = client.get(MATRIX).json()
    pair_id = matrix["rows"][0]["meta"]["pair_id"]
    week = next(w["key"] for w in matrix["weeks"] if not w["is_closed"])

    def stamps() -> tuple[int, int]:
        with SessionLocal() as session:
            return (
                session.get(StoreProduct, pair_id).calc_revision,
                session.get(AppSettings, 1).calc_epoch,
            )

    revision_before, epoch_before = stamps()
    edit = client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 55})
    assert edit.status_code == 200, edit.text
    try:
        revision_after, epoch_after = stamps()

        assert revision_after > revision_before
        assert epoch_after == epoch_before, "правка одной пары не сбрасывает остальные"
    finally:
        client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})
