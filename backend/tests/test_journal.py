"""Журнал корректировок (задача G-2).

Главное, что проверяется: массовая правка читается как одно действие, а не как
поток из сотен несвязанных записей, и по этой записи можно развернуть её ячейки.
"""

from __future__ import annotations


def pick(client, count=3):
    payload = client.get("/api/v1/matrix?level=sku").json()
    rows = payload["rows"][:count]
    weeks = [w["key"] for w in payload["weeks"] if not w["is_closed"]][:4]
    return [r["meta"]["pair_id"] for r in rows], weeks


def test_entries_carry_product_names(client):
    """В журнале видно товар и магазин, а не только номер пары."""
    pair_ids, weeks = pick(client, 1)
    client.put(
        f"/api/v1/forecast/{pair_ids[0]}/{weeks[0]}",
        json={"manual_forecast": 321, "author": "снабженец"},
    )

    page = client.get(f"/api/v1/forecast/log?pair_id={pair_ids[0]}").json()
    assert page["total"] >= 1
    entry = page["entries"][0]

    assert entry["title"]
    assert entry["vendor_code"]
    assert entry["store_name"]
    assert entry["field_label"] == "Прогноз"
    assert entry["operation"] == "manual"
    assert entry["batch_id"] is None
    assert entry["new_value"] == 321
    assert entry["author"] == "снабженец"

    client.put(f"/api/v1/forecast/{pair_ids[0]}/{weeks[0]}", json={"clear": ["manual_forecast"]})


def test_bulk_edit_collapses_into_one_operation(client):
    """Сотня записей одной правки сворачивается в одну строку журнала."""
    pair_ids, weeks = pick(client, 3)
    before = client.get("/api/v1/forecast/log/batches").json()["total"]

    result = client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": pair_ids,
            "weeks": weeks,
            "operation": "scale",
            "value": 130,
            "author": "старший снабженец",
        },
    ).json()
    batch_id = result["batch_id"]
    assert batch_id

    batches = client.get("/api/v1/forecast/log/batches").json()
    assert batches["total"] == before + 1

    batch = next(b for b in batches["batches"] if b["batch_id"] == batch_id)
    assert batch["operation"] == "scale"
    assert batch["author"] == "старший снабженец"
    assert batch["cells"] == result["updated_cells"]
    assert batch["pairs"] == len(pair_ids)
    assert batch["week_from"] == weeks[0]
    assert batch["week_to"] == weeks[-1]

    # По идентификатору операции разворачиваются её ячейки.
    entries = client.get(f"/api/v1/forecast/log?batch_id={batch_id}&limit=500").json()
    assert entries["total"] == batch["cells"]
    assert {e["week"] for e in entries["entries"]} == set(weeks)
    assert all(e["operation"] == "scale" for e in entries["entries"])

    client.post(
        "/api/v1/forecast/bulk",
        json={"pair_ids": pair_ids, "weeks": weeks, "operation": "clear"},
    )


def test_single_edits_are_not_batches(client):
    """Поячеечные правки не попадают в режим «по операциям»: сворачивать их не во что."""
    pair_ids, weeks = pick(client, 1)
    before = client.get("/api/v1/forecast/log/batches").json()["total"]

    client.put(f"/api/v1/forecast/{pair_ids[0]}/{weeks[1]}", json={"diff_percent": 90})
    after = client.get("/api/v1/forecast/log/batches").json()["total"]
    assert after == before

    page = client.get(f"/api/v1/forecast/log?pair_id={pair_ids[0]}&field=diff_percent").json()
    assert page["total"] >= 1
    assert page["entries"][0]["field_label"] == "DIFF"

    client.put(f"/api/v1/forecast/{pair_ids[0]}/{weeks[1]}", json={"clear": ["diff_percent"]})


def test_filters_narrow_the_journal(client):
    pair_ids, weeks = pick(client, 2)
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": pair_ids,
            "weeks": weeks,
            "operation": "set",
            "value": 7,
            "author": "фильтруемый",
        },
    )

    rows = client.get("/api/v1/matrix?level=sku").json()["rows"]
    target = next(r for r in rows if r["meta"]["pair_id"] == pair_ids[0])
    store_id = target["meta"]["store_id"]

    by_author = client.get("/api/v1/forecast/log?author=фильтруемый&limit=500").json()
    assert by_author["total"] > 0
    assert all(e["author"] == "фильтруемый" for e in by_author["entries"])

    by_store = client.get(
        f"/api/v1/forecast/log?author=фильтруемый&store_id={store_id}&limit=500"
    ).json()
    assert 0 < by_store["total"] <= by_author["total"]

    by_week = client.get(
        f"/api/v1/forecast/log?author=фильтруемый&week_from={weeks[1]}&week_to={weeks[1]}"
    ).json()
    assert by_week["total"] > 0
    assert {e["week"] for e in by_week["entries"]} == {weeks[1]}

    nothing = client.get("/api/v1/forecast/log?author=такого-автора-нет").json()
    assert nothing["total"] == 0
    assert nothing["entries"] == []

    client.post(
        "/api/v1/forecast/bulk",
        json={"pair_ids": pair_ids, "weeks": weeks, "operation": "clear"},
    )


def test_pagination_walks_the_whole_journal(client):
    pair_ids, weeks = pick(client, 3)
    client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": pair_ids,
            "weeks": weeks,
            "operation": "set",
            "value": 11,
            "author": "страничный",
        },
    )

    first = client.get("/api/v1/forecast/log?author=страничный&limit=5&offset=0").json()
    assert first["total"] >= 6
    assert len(first["entries"]) == 5

    second = client.get("/api/v1/forecast/log?author=страничный&limit=5&offset=5").json()
    assert second["total"] == first["total"]

    ids = {e["id"] for e in first["entries"]} | {e["id"] for e in second["entries"]}
    assert len(ids) == len(first["entries"]) + len(second["entries"])

    client.post(
        "/api/v1/forecast/bulk",
        json={"pair_ids": pair_ids, "weeks": weeks, "operation": "clear"},
    )


def test_journal_records_old_and_new_value(client):
    """Запись должна давать разобрать правку: что было и что стало."""
    pair_ids, weeks = pick(client, 1)
    pair_id, week = pair_ids[0], weeks[2]

    client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 100})
    client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"manual_forecast": 250})

    page = client.get(
        f"/api/v1/forecast/log?pair_id={pair_id}&field=manual_forecast&limit=10"
    ).json()
    latest = page["entries"][0]
    assert latest["old_value"] == 100
    assert latest["new_value"] == 250

    client.put(f"/api/v1/forecast/{pair_id}/{week}", json={"clear": ["manual_forecast"]})
    reset_entry = client.get(
        f"/api/v1/forecast/log?pair_id={pair_id}&field=manual_forecast&limit=10"
    ).json()["entries"][0]
    assert reset_entry["old_value"] == 250
    assert reset_entry["new_value"] is None


def test_move_batch_spans_sources_and_target(client):
    """У переноса в журнале видно и недели-источники, и целевую."""
    pair_ids, weeks = pick(client, 1)
    payload = client.get("/api/v1/matrix?level=sku").json()
    future = [w["key"] for w in payload["weeks"] if not w["is_closed"]]
    target = future[8]

    result = client.post(
        "/api/v1/forecast/bulk",
        json={
            "pair_ids": pair_ids,
            "weeks": weeks,
            "operation": "move",
            "target_week": target,
            "author": "перенос",
        },
    ).json()

    batch = next(
        b
        for b in client.get("/api/v1/forecast/log/batches").json()["batches"]
        if b["batch_id"] == result["batch_id"]
    )
    assert batch["operation"] == "move"
    assert batch["week_from"] == weeks[0]
    assert batch["week_to"] == target

    client.post(
        "/api/v1/forecast/bulk",
        json={"pair_ids": pair_ids, "weeks": weeks + [target], "operation": "clear"},
    )
