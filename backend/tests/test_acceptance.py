"""Тест-кейсы приёмки (задача C-12).

Это не ещё один слой тестов, а исполняемая версия документа
`docs/05-test-cases.md`: каждый кейс там имеет номер ПР-N, и здесь ему
соответствует ровно один тест с тем же номером. Документ, который расходится
с поведением, бесполезен на приёмке — а расходиться он может только вместе
с падающим тестом.

Кейсы идут от лица человека, а не от кода: вход через API на демонстрационных
данных, проверка того, что увидит заказчик на экране. Числа в проверках — наши:
пороги статусов и определение ROI ещё не согласованы (вопросы Q-1, Q-2),
поэтому кейсы проверяют **правила**, а не конкретные значения заказчика,
и там, где правило — допущение, об этом сказано прямо.

Запуск набора: `pytest -k acceptance`, одного кейса: `pytest -k pr_05`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from io import BytesIO

import pytest
from openpyxl import load_workbook
from pytest import approx
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Store, SyncRun, SyncStatus
from app.services.matrix_import import HEADER_MARKER, SKU_COLUMNS

MATRIX = "/api/v1/matrix"
PURCHASE = "/api/v1/purchase"
CATALOG = "/api/v1/catalog"


def _matrix(client, level: str = "sku", **params) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items())
    response = client.get(f"{MATRIX}?level={level}&{query}")
    assert response.status_code == 200, response.text
    return response.json()


def _weeks(payload: dict) -> list[str]:
    return [week["key"] for week in payload["weeks"] if not week["is_closed"]]


def _num(value) -> float:
    """Пустая ячейка показателя — это ноль: факта будущей недели ещё нет."""
    return float(value or 0)


def _row_with_demand(client) -> tuple[dict, str]:
    """Позиция, которая что-то продаёт, и ближайшая открытая неделя."""
    payload = _matrix(client)
    week = _weeks(payload)[0]
    row = next(r for r in payload["rows"] if r["metrics"]["original_forecast"][week] > 0)
    return row, week


def _clear(client, pair_id: int, week: str) -> None:
    client.put(
        f"/api/v1/forecast/{pair_id}/{week}",
        json={"clear": ["manual_forecast", "diff_percent", "manual_adapted"]},
    )


# ------------------------------------------------- критерий № 1: кабинеты


def test_pr_01_cabinets_never_mix(client):
    """ПР-1. Данные кабинетов не смешиваются (критерий приёмки № 1).

    Дано: три кабинета с общими товарами. Когда: загрузку повторяют и смотрят
    матрицу по каждому кабинету. Тогда: строка принадлежит своему кабинету,
    сумма по кабинетам равна всей выборке, а повторная загрузка не удваивает
    факты — ключ факта это тройка (магазин, товар, неделя).
    """
    stores = client.get(f"{CATALOG}/stores").json()

    def facts() -> float:
        payload = _matrix(client)
        return sum(
            _num(row["metrics"]["fact_sales"][week])
            for row in payload["rows"]
            for week in payload["totals"]["metrics"]["fact_sales"]
        )

    # Сравниваются две одинаковые загрузки, а не загрузка с состоянием сида:
    # сид грузит своё окно дней, и разница в окне — это не двойной учёт.
    assert client.post("/api/v1/sync/wb").status_code == 200
    fact_before = facts()
    assert client.post("/api/v1/sync/wb").status_code == 200
    fact_after = facts()

    assert fact_after == approx(fact_before), "повторная загрузка удвоила факты"
    after = _matrix(client)

    counted = 0
    for store in stores:
        rows = _matrix(client, store_id=store["id"])["rows"]
        assert all(row["meta"]["store_id"] == store["id"] for row in rows)
        counted += len(rows)
    assert counted == len(after["rows"])


# ------------------------------------------- критерий № 2: правка прогноза


def test_pr_02_manual_forecast_reaches_stock_and_purchase_and_survives(client):
    """ПР-2. Правка прогноза пересчитывает сток и закупку и переживает пересчёт.

    Дано: позиция с прогнозом. Когда: человек ставит своё число в ячейку.
    Тогда: сток и закупочная потребность считаются уже по нему, а повторное
    чтение матрицы (то есть полный пересчёт) правку не стирает — критерий № 2.
    """
    row, week = _row_with_demand(client)
    pair_id = row["meta"]["pair_id"]
    was = row["metrics"]["original_forecast"][week]
    stock_before = row["metrics"]["real_stock"][week]

    answer = client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": was + 500})
    assert answer.status_code == 200, answer.text
    try:
        edited = next(r for r in _matrix(client)["rows"] if r["meta"]["pair_id"] == pair_id)
        assert edited["metrics"]["original_forecast"][week] == approx(was + 500)
        assert edited["metrics"]["real_stock"][week] < stock_before, "сток не пересчитался"

        # Пересчёт её не трогает: число остаётся тем, что поставил человек.
        again = next(r for r in _matrix(client)["rows"] if r["meta"]["pair_id"] == pair_id)
        assert again["metrics"]["original_forecast"][week] == approx(was + 500)

        in_purchase = client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
        mine = next(r for r in in_purchase if r["pair_id"] == pair_id)
        assert mine["qty"] > 0, "возросший спрос должен проситься в закупку"
    finally:
        _clear(client, pair_id, week)

    restored = next(r for r in _matrix(client)["rows"] if r["meta"]["pair_id"] == pair_id)
    assert restored["metrics"]["original_forecast"][week] == approx(was)


# --------------------------------------------- критерий № 3: статусы и DIFF


def test_pr_03_statuses_follow_the_rules_thresholds_pending(client):
    """ПР-3. Статусы OVER/SHORT и DIFF следуют правилам (критерий приёмки № 3).

    Дано: выборка с позициями в дефиците и в избытке. Когда: смотрят значки
    строк и отбор по статусу. Тогда: отбор возвращает ровно те строки, у которых
    этот статус, у SHORT в горизонте есть непокрытый спрос, а DIFF меняет
    исходный прогноз пропорционально.

    **[ДОПУЩЕНИЕ, Q-1]** Сами пороги (8 недель на SHORT, 12 недель покрытия
    на OVER) и трактовка DIFF как процентного множителя — наши; их и нужно
    сверить с действующими файлами заказчика. Правило проверяется, число — нет.
    """
    everything = _matrix(client)
    weeks = _weeks(everything)

    for status in ("SHORT", "OVER", "OK"):
        picked = _matrix(client, status=status)["rows"]
        assert all(row["status"] == status for row in picked)
        assert len(picked) == sum(1 for row in everything["rows"] if row["status"] == status)

    short_rows = [row for row in everything["rows"] if row["status"] == "SHORT"]
    assert short_rows, "в демо-данных должен быть дефицит"
    for row in short_rows:
        assert any(row["metrics"]["shortage_units"][week] > 0 for week in weeks)

    row, week = _row_with_demand(client)
    pair_id = row["meta"]["pair_id"]
    was = row["metrics"]["original_forecast"][week]
    assert client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"diff_percent": 50}).status_code
    try:
        changed = next(r for r in _matrix(client)["rows"] if r["meta"]["pair_id"] == pair_id)
        # Прогноз округляется до штук, поэтому половина от 57 — это 29, а не 28,5.
        assert changed["metrics"]["original_forecast"][week] == approx(was * 0.5, abs=1)
    finally:
        _clear(client, pair_id, week)


# ------------------------------------------------ критерий № 4: агрегация


def test_pr_04_levels_agree_and_stock_is_not_counted_twice(client):
    """ПР-4. Итоги трёх уровней сходятся, остаток не учитывается дважды.

    Дано: один и тот же набор позиций. Когда: его смотрят по магазинам,
    категориям и артикулам. Тогда: итог недели одинаков на всех уровнях,
    и сумма строк равна итогу — критерий приёмки № 4.
    """
    levels = {level: _matrix(client, level=level) for level in ("store", "category", "sku")}
    weeks = [week["key"] for week in levels["sku"]["weeks"]]

    for metric in ("original_forecast", "adapted_forecast", "real_stock", "fact_sales"):
        for week in weeks:
            totals = []
            for payload in levels.values():
                total = _num(payload["totals"]["metrics"][metric][week])
                rows_sum = sum(_num(r["metrics"][metric][week]) for r in payload["rows"])
                assert rows_sum == approx(total), (metric, week)
                totals.append(total)
            assert max(totals) - min(totals) < 1e-6, (metric, week)


# ------------------------------------------------------- закупка и заказы


def test_pr_05_purchase_becomes_an_order_and_the_need_drops(client):
    """ПР-5. Потребность → заказ → товар в пути → потребность уменьшилась.

    Дано: позиция, которую расчёт просит купить. Когда: по ней собирают заказ
    и размещают его. Тогда: появляется поставка «в пути», и та же позиция
    больше не просит столько же; отмена заказа возвращает потребность.
    """
    rows = client.get(f"{PURCHASE}?only_needed=true").json()["rows"]
    row = next(r for r in rows if r["qty"] > 0 and r["arrival_week"])
    pair_id, qty = row["pair_id"], row["qty"]

    created = client.post(
        "/api/v1/orders",
        json={"lines": [{"pair_id": pair_id, "qty": qty, "arrival_week": row["arrival_week"]}]},
    )
    assert created.status_code == 201, created.text
    order = created.json()
    try:
        # Черновик расчёту не виден: собранный, но не отправленный заказ — ещё не заказ.
        draft_rows = client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
        assert next(r for r in draft_rows if r["pair_id"] == pair_id)["qty"] == qty

        placed = client.post(f"/api/v1/orders/{order['id']}/status", json={"status": "placed"})
        assert placed.status_code == 200, placed.text

        after = next(
            r
            for r in client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
            if r["pair_id"] == pair_id
        )
        assert after["qty"] < qty, "размещённый заказ должен уменьшить потребность"
        week = row["arrival_week"]
        supplies = next(r for r in _matrix(client)["rows"] if r["meta"]["pair_id"] == pair_id)
        assert supplies["metrics"]["supply"][week] >= qty, "товар в пути не виден в матрице"
    finally:
        client.post(f"/api/v1/orders/{order['id']}/status", json={"status": "cancelled"})

    back = next(
        r
        for r in client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
        if r["pair_id"] == pair_id
    )
    assert back["qty"] == approx(qty), "отмена вернула потребность"


def test_pr_06_price_change_reaches_purchase_but_not_a_placed_order(client):
    """ПР-6. Новая цена меняет закупку, но не сумму уже размещённого заказа.

    Дано: позиция с себестоимостью. Когда: записывают новую цену. Тогда:
    закупка считается по новой, а строка заказа держит снимок прежней — сумма
    отправленного документа не меняется задним числом, но расхождение видно.
    """
    rows = client.get(f"{PURCHASE}?only_needed=true").json()["rows"]
    row = next(r for r in rows if r["qty"] > 0 and r["arrival_week"])
    product = next(
        p
        for p in client.get(f"{CATALOG}/products").json()
        if p["vendor_code"] == row["vendor_code"]
    )
    was = product["cost_price"]

    created = client.post(
        "/api/v1/orders",
        json={
            "lines": [
                {
                    "pair_id": row["pair_id"],
                    "qty": row["qty"],
                    "arrival_week": row["arrival_week"],
                }
            ]
        },
    )
    assert created.status_code == 201, created.text
    order = created.json()
    added = client.post(
        f"{CATALOG}/products/{product['id']}/prices",
        json={"valid_from": date.today().isoformat(), "cost_price": was + 100},
    )
    assert added.status_code == 201, added.text
    price_id = added.json()["history"][0]["id"]
    try:
        in_purchase = next(
            r
            for r in client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
            if r["pair_id"] == row["pair_id"]
        )
        assert in_purchase["params"]["cost_price"] == approx(was + 100)

        line = client.get(f"/api/v1/orders/{order['id']}").json()["lines"][0]
        assert line["cost_price"] == approx(was), "снимок в документе переписывать нельзя"
        assert line["cost_price_now"] == approx(was + 100), "расхождение должно быть видно"
    finally:
        client.delete(f"{CATALOG}/products/{product['id']}/prices/{price_id}")
        client.post(f"/api/v1/orders/{order['id']}/status", json={"status": "cancelled"})


# ------------------------------------------------------------ справочники


def test_pr_07_new_position_starts_being_calculated(client):
    """ПР-7. Заведённая позиция считается, снятая с продажи — нет.

    Дано: пустой каталог (в кейсе — новый товар). Когда: его заводят и ставят
    в кабинет. Тогда: позиция появляется в матрице и в закупке; когда товар
    снимают с продажи, она исчезает из расчёта, а её прошлое остаётся.
    """
    store = client.get(f"{CATALOG}/stores").json()[0]
    was = _matrix(client)["context"]["sku_count"]

    product = client.post(
        f"{CATALOG}/products",
        json={"vendor_code": "ПР-07", "title": "Позиция кейса приёмки", "cost_price": 100},
    ).json()
    pairs = client.post(
        f"{CATALOG}/products/{product['id']}/stores",
        json={"store_id": store["id"], "nm_id": "990007", "base_potential": 20},
    ).json()
    try:
        assert _matrix(client)["context"]["sku_count"] == was + 1
        assert any(
            r["vendor_code"] == "ПР-07"
            for r in client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
        )

        client.put(f"{CATALOG}/products/{product['id']}", json={"is_active": False})
        assert _matrix(client)["context"]["sku_count"] == was, "снятый товар остался в расчёте"
    finally:
        client.delete(f"{CATALOG}/pairs/{pairs[0]['id']}")
        client.delete(f"{CATALOG}/products/{product['id']}")

    assert _matrix(client)["context"]["sku_count"] == was


# ------------------------------------------------------------------ права


def test_pr_08_viewer_reads_everything_and_changes_nothing(client, viewer_client):
    """ПР-8. Роль «просмотр» видит данные, но ничего не меняет.

    Дано: человек с правом просмотра. Когда: он открывает экраны и пробует
    править. Тогда: чтение работает везде, а любая попытка записи — 403,
    и проверка эта на сервере, а не в спрятанной кнопке.
    """
    row, week = _row_with_demand(client)
    pair_id = row["meta"]["pair_id"]
    product = client.get(f"{CATALOG}/products").json()[0]

    for path in (f"{MATRIX}?level=sku", f"{PURCHASE}?only_needed=false", f"{CATALOG}/products"):
        assert viewer_client.get(path).status_code == 200

    assert (
        viewer_client.put(
            f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 1}
        ).status_code
        == 403
    )
    assert viewer_client.post("/api/v1/orders", json={"lines": []}).status_code == 403
    assert (
        viewer_client.post(
            f"{CATALOG}/products/{product['id']}/prices",
            json={"valid_from": date.today().isoformat(), "cost_price": 1},
        ).status_code
        == 403
    )
    assert viewer_client.post("/api/v1/sync/wb").status_code == 403


# ------------------------------------------------------- выгрузка и возврат


def test_pr_09_matrix_goes_to_excel_and_comes_back(client):
    """ПР-9. Выгрузка в Excel и возврат правок из файла.

    Дано: матрица на экране. Когда: её выгружают, правят число в файле
    и загружают файл обратно. Тогда: система показывает, что именно изменится,
    и после подтверждения число из файла становится прогнозом.
    """
    row, week = _row_with_demand(client)
    pair_id = row["meta"]["pair_id"]
    was = row["metrics"]["original_forecast"][week]

    exported = client.get(f"{MATRIX}/export?level=sku")
    assert exported.status_code == 200, exported.text
    book = load_workbook(BytesIO(exported.content))
    sheet = book.worksheets[0]

    # Строку заголовков таблицы ищем так же, как её ищет загрузка: по колонке
    # «Показатель». Выше неё в книге стоит блок подписей, и в нём тоже есть
    # строка, начинающаяся со слова «Магазин».
    head = next(
        index
        for index, line in enumerate(sheet.iter_rows(values_only=True), start=1)
        if line and HEADER_MARKER in [cell for cell in line[: len(SKU_COLUMNS)]]
    )
    year, number = week.split("-W")
    week_column = next(
        cell.column
        for cell in sheet[head]
        if str(cell.value or "").split() == [f"W{int(number)}", year]
    )
    target = next(
        line
        for line in sheet.iter_rows(min_row=head + 1)
        if line[1].value == row["meta"]["vendor_code"]
        and line[0].value == row["meta"]["store_name"]
        and line[5].value == "План / исходный прогноз"
    )
    sheet.cell(row=target[0].row, column=week_column, value=was + 250)
    changed = BytesIO()
    book.save(changed)
    payload = changed.getvalue()

    files = {"file": ("матрица.xlsx", payload, "application/vnd.ms-excel")}
    preview = client.post(f"{MATRIX}/import?apply=false", files=files)
    assert preview.status_code == 200, preview.text
    assert preview.json()["change_count"] == 1, "в файле изменено ровно одно число"

    try:
        applied = client.post(
            f"{MATRIX}/import?apply=true",
            files={"file": ("матрица.xlsx", payload, "application/vnd.ms-excel")},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["applied_cells"] == 1

        after = next(r for r in _matrix(client)["rows"] if r["meta"]["pair_id"] == pair_id)
        assert after["metrics"]["original_forecast"][week] == approx(was + 250)
    finally:
        _clear(client, pair_id, week)


# ------------------------------------------------------ сопровождение


def test_pr_10_failed_load_is_visible_from_outside(client, anon_client):
    """ПР-10. Сбой загрузки данных виден, а не прячется в журнале.

    Дано: кабинет, у которого последняя загрузка упала. Когда: систему
    опрашивает мониторинг и открывает человек. Тогда: готовность отвечает 503,
    а состояние загрузки называет кабинет и причину. Живость при этом зелёная:
    по ней перезапускают процесс, а перезапуск данных в кабинете не поправит.
    """
    with SessionLocal() as session:
        store_id = session.scalars(select(Store).order_by(Store.code)).first().id
        run = SyncRun(
            store_id=store_id,
            status=SyncStatus.FAILED,
            message="HTTPError: 401 Unauthorized",
            finished_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    try:
        assert anon_client.get("/api/v1/health").status_code == 200
        ready = anon_client.get("/api/v1/health/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"]["sync"] == "failed"

        status = client.get("/api/v1/sync/status").json()
        assert status["status"] == "degraded"
        broken = next(s for s in status["stores"] if s["store_id"] == store_id)
        assert broken["status"] == "failed"
        assert "401" in broken["message"]
    finally:
        with SessionLocal() as session:
            session.delete(session.get(SyncRun, run_id))
            session.commit()

    assert anon_client.get("/api/v1/health/ready").status_code == 200


def test_pr_11_roi_compares_positions_but_orders_nothing(client):
    """ПР-11. ROI сравнивает позиции, но ни на что не влияет.

    Дано: позиция в плане закупки. Когда: у товара меняется цена продажи.
    Тогда: ROI меняется, а количество, дата заказа и неделя прихода — нет;
    у позиции без заказа ROI пуст, а не нулевой. В выгрузке та же колонка.

    **[ДОПУЩЕНИЕ, Q-2]** Определение ROI (годовая доходность вложенных денег
    по валовой марже, без комиссии маркетплейса и логистики) — наше.
    """
    rows = client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
    row = next(r for r in rows if r["qty"] > 0 and r["roi_percent"])
    product = next(
        p
        for p in client.get(f"{CATALOG}/products").json()
        if p["vendor_code"] == row["vendor_code"]
    )
    assert row["sell_out_weeks"] > 0, "число должно объясняться оборотом"
    assert all(
        r["roi_percent"] is None for r in rows if r["qty"] == 0
    ), "без заказа ROI пуст, а не ноль"

    added = client.post(
        f"{CATALOG}/products/{product['id']}/prices",
        json={"valid_from": date.today().isoformat(), "price": product["price"] * 2},
    )
    assert added.status_code == 201, added.text
    price_id = added.json()["history"][0]["id"]
    try:
        after = next(
            r
            for r in client.get(f"{PURCHASE}?only_needed=false").json()["rows"]
            if r["pair_id"] == row["pair_id"]
        )
        assert after["roi_percent"] > row["roi_percent"], "дороже продаём — выше доходность"
        assert after["qty"] == row["qty"], "ROI не должен менять заказ"
        assert after["order_date"] == row["order_date"]
        assert after["arrival_week"] == row["arrival_week"]
    finally:
        client.delete(f"{CATALOG}/products/{product['id']}/prices/{price_id}")

    exported = client.get(f"{PURCHASE}/export?only_needed=true")
    assert exported.status_code == 200
    sheet = load_workbook(BytesIO(exported.content)).worksheets[0]
    assert any(
        "ROI" in str(cell.value or "") for line in sheet.iter_rows(max_row=12) for cell in line
    ), "в файле закупки ROI тоже должен быть"


@pytest.mark.parametrize(
    "case",
    [f"pr_{number:02d}" for number in range(1, 12)],
)
def test_every_case_from_the_document_is_runnable(case):
    """Каждый номер из `docs/05-test-cases.md` должен существовать здесь.

    Иначе документ приёмки начнёт расходиться с тем, что на самом деле
    проверяется, — и разойдётся молча.
    """
    names = {name for name in globals() if name.startswith("test_")}
    assert any(case in name for name in names), f"кейс {case} описан, но не реализован"
