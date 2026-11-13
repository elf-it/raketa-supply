"""Сквозные проверки API на демо-данных."""

from __future__ import annotations


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_catalog_is_seeded(client):
    stores = client.get("/api/v1/catalog/stores").json()
    categories = client.get("/api/v1/catalog/categories").json()
    products = client.get("/api/v1/catalog/products").json()

    assert len(stores) == 3
    assert {s["code"] for s in stores} == {"store01", "store02", "store03"}
    assert len(categories) == 4
    assert len(products) == 12


def test_matrix_levels_return_expected_shape(client):
    for level, expected_rows in (("store", 3), ("category", 4)):
        payload = client.get(f"/api/v1/matrix?level={level}").json()
        assert payload["level"] == level
        assert len(payload["rows"]) == expected_rows
        assert payload["weeks"], "ось недель пустая"
        assert "original_forecast" in payload["totals"]["metrics"]

    sku = client.get("/api/v1/matrix?level=sku").json()
    assert len(sku["rows"]) == 18  # пар (магазин, товар) в демо-данных
    assert sku["rows"][0]["meta"]["editable"] is True


def test_totals_match_across_levels(client):
    """Критерий приёмки № 4 — на реальных данных, а не только в юнит-тесте."""
    levels = {
        level: client.get(f"/api/v1/matrix?level={level}").json()
        for level in ("store", "category", "sku")
    }
    weeks = [w["key"] for w in levels["sku"]["weeks"]]

    for metric in ("original_forecast", "adapted_forecast", "adapted_stock", "real_stock"):
        for week in weeks:
            values = []
            for payload in levels.values():
                total = payload["totals"]["metrics"][metric][week]
                rows_sum = sum(row["metrics"][metric][week] for row in payload["rows"])
                assert abs(rows_sum - total) < 1e-6, (metric, week)
                values.append(total)
            assert max(values) - min(values) < 1e-6, (metric, week)


def test_store_filter_does_not_mix_cabinets(client):
    """Критерий приёмки № 1: данные кабинетов не смешиваются."""
    stores = client.get("/api/v1/catalog/stores").json()
    all_rows = client.get("/api/v1/matrix?level=sku").json()["rows"]
    per_store = 0
    for store in stores:
        rows = client.get(f"/api/v1/matrix?level=sku&store_id={store['id']}").json()["rows"]
        assert all(r["meta"]["store_id"] == store["id"] for r in rows)
        per_store += len(rows)
    assert per_store == len(all_rows)


def test_manual_forecast_survives_recalculation(client):
    """Критерий приёмки № 2."""
    payload = client.get("/api/v1/matrix?level=sku").json()
    row = payload["rows"][0]
    pair_id = row["meta"]["pair_id"]
    week = next(w["key"] for w in payload["weeks"] if not w["is_closed"])

    response = client.put(
        f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 777, "author": "tester"}
    )
    assert response.status_code == 200

    after = client.get("/api/v1/matrix?level=sku").json()
    changed = next(r for r in after["rows"] if r["meta"]["pair_id"] == pair_id)
    assert changed["metrics"]["original_forecast"][week] == 777
    assert changed["metrics"]["is_manual"][week] is True

    log = client.get(f"/api/v1/forecast/log?pair_id={pair_id}").json()
    assert any(entry["field"] == "manual_forecast" and entry["new_value"] == 777 for entry in log)

    # Сброс возвращает расчётное значение.
    client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})
    restored = client.get("/api/v1/matrix?level=sku").json()
    cell = next(r for r in restored["rows"] if r["meta"]["pair_id"] == pair_id)
    assert cell["metrics"]["original_forecast"][week] != 777


def test_forecast_change_recalculates_stock_and_purchase(client):
    """Критерий приёмки № 2: правка прогноза тянет за собой остатки и закупку."""
    payload = client.get("/api/v1/matrix?level=sku").json()
    row = payload["rows"][0]
    pair_id = row["meta"]["pair_id"]
    future = [w["key"] for w in payload["weeks"] if not w["is_closed"]]
    week = future[0]
    last_week = future[-1]

    before_stock = row["metrics"]["real_stock"][last_week]
    before_purchase = client.get(f"/api/v1/purchase?product_id={row['meta']['product_id']}").json()

    client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 5000})
    after = client.get("/api/v1/matrix?level=sku").json()
    changed = next(r for r in after["rows"] if r["meta"]["pair_id"] == pair_id)

    assert changed["metrics"]["real_stock"][last_week] < before_stock
    after_purchase = client.get(f"/api/v1/purchase?product_id={row['meta']['product_id']}").json()
    assert after_purchase["total_qty"] > before_purchase["total_qty"]

    client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})


def test_purchase_plan_respects_pack_size(client):
    plan = client.get("/api/v1/purchase?only_needed=true").json()
    assert plan["rows"], "ни одной позиции к заказу — проверьте демо-данные"
    for row in plan["rows"]:
        pack = row["params"]["pack_size"]
        assert row["qty"] % pack == 0, row["vendor_code"]
        assert row["qty"] >= row["params"]["moq"]
        assert row["order_date"] is not None


def test_sync_is_idempotent(client):
    first = client.post("/api/v1/sync/wb?days=140").json()
    second = client.post("/api/v1/sync/wb?days=140").json()
    assert [r["store_code"] for r in first] == [r["store_code"] for r in second]
    assert all(r["error"] is None for r in second)
    assert [r["facts_upserted"] for r in first] == [r["facts_upserted"] for r in second]

    runs = client.get("/api/v1/sync/runs").json()
    assert runs and runs[0]["status"] == "success"


def test_base_potential_carries_fact_reference(client):
    """Потенциал задаётся не вслепую: рядом факт закрытых недель и оценка по нему."""
    rows = client.get("/api/v1/reference/base-potential?fact_weeks=8").json()
    assert len(rows) == 18

    with_facts = [r for r in rows if r["fact_weeks"] > 0]
    assert with_facts, "ни по одной позиции нет факта — проверьте демо-данные"
    for row in with_facts:
        assert row["fact_weeks"] <= 8
        assert row["avg_fact_units"] is not None
        assert row["excluded_weeks"] <= row["fact_weeks"]

    # Оценка строится обратным ходом из факта и сезонности, значит при коэффициенте
    # выше единицы она ниже среднего факта, и наоборот.
    estimated = [r for r in with_facts if r["suggested_potential"] is not None]
    assert estimated

    narrow = client.get("/api/v1/reference/base-potential?fact_weeks=2").json()
    assert max(r["fact_weeks"] for r in narrow) <= 2


def test_reference_pages(client):
    potentials = client.get("/api/v1/reference/base-potential").json()
    assert len(potentials) == 18

    pair_id = potentials[0]["pair_id"]
    updated = client.put(
        f"/api/v1/reference/base-potential/{pair_id}", json={"base_potential": 321}
    ).json()
    assert updated["base_potential"] == 321
    assert updated["pair_id"] == pair_id
    assert updated["fact_weeks"] == potentials[0]["fact_weeks"]

    seasonality = client.get("/api/v1/reference/seasonality").json()
    assert len(seasonality) == 4
    category_id = seasonality[0]["category_id"]
    result = client.put(
        f"/api/v1/reference/seasonality/{category_id}", json={"coefficients": {"7": 1.75}}
    ).json()
    assert result["coefficients"]["7"] == 1.75

    bad = client.put(
        f"/api/v1/reference/seasonality/{category_id}", json={"coefficients": {"99": 1.0}}
    )
    assert bad.status_code == 400
