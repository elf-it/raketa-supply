"""Логи, номер запроса и готовность (задача G-4).

Две главные проверки. Первая — что в лог не попадает то, чего там быть не должно:
лог читают шире, чем базу, и пароль или почта, уехавшие в сборщик логов, оттуда
уже не возвращаются. Вторая — что сбой загрузки виден снаружи: готовность должна
отвечать 503, иначе о нём никто не узнает, пока кто-нибудь не откроет журнал.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from conftest import EDITOR
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Store, SyncRun, SyncStatus
from app.observability import (
    REQUEST_ID_HEADER,
    JsonFormatter,
    RequestLogMiddleware,
    mask_email,
)

HEALTH = "/api/v1/health"
READY = "/api/v1/health/ready"
STATUS = "/api/v1/sync/status"


def _records(caplog) -> str:
    return "\n".join(
        record.getMessage() + str(getattr(record, "fields", "")) for record in caplog.records
    )


@pytest.fixture
def failed_run(client):
    """Провалившийся прогон последнего по алфавиту кабинета — и уборка за собой."""
    with SessionLocal() as session:
        store_id = session.scalars(select(Store).order_by(Store.code)).first().id
        run = SyncRun(
            store_id=store_id,
            status=SyncStatus.FAILED,
            message="RuntimeError: проверка",
            finished_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    yield store_id
    with SessionLocal() as session:
        session.delete(session.get(SyncRun, run_id))
        session.commit()


def test_every_answer_carries_a_request_number(client):
    first = client.get(HEALTH)
    second = client.get(HEALTH)

    assert first.headers[REQUEST_ID_HEADER]
    assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]


def test_number_from_the_proxy_is_kept_and_junk_is_not(client):
    """Свой номер прокси сохраняется: иначе один запрос получит два номера."""
    kept = client.get(HEALTH, headers={REQUEST_ID_HEADER: "edge-7f3a-01"})
    junk = client.get(HEALTH, headers={REQUEST_ID_HEADER: "<script>alert(1)</script>"})

    assert kept.headers[REQUEST_ID_HEADER] == "edge-7f3a-01"
    assert junk.headers[REQUEST_ID_HEADER] != "<script>alert(1)</script>"
    assert junk.headers[REQUEST_ID_HEADER].isalnum()


def test_request_line_says_what_happened_and_who_did_it(client, caplog):
    with caplog.at_level(logging.INFO, logger="raketa"):
        answer = client.get(f"{HEALTH}")
        line = next(r for r in caplog.records if r.getMessage().startswith("GET /api/v1/health"))

    assert line.fields["status"] == 200
    assert int(line.fields["ms"]) >= 0
    assert line.request_id == answer.headers[REQUEST_ID_HEADER]


def test_who_made_the_request_is_an_identifier_not_an_email(client, caplog):
    with caplog.at_level(logging.INFO, logger="raketa"):
        client.get("/api/v1/catalog/stores")
        line = next(r for r in caplog.records if "/api/v1/catalog/stores" in r.getMessage())

    assert line.actor.endswith("/editor"), line.actor
    assert EDITOR[0] not in _records(caplog), "почта в лог не уходит"


def test_password_never_reaches_the_log(client, caplog):
    """Даже неверный пароль не пишется: в нём бывает чужой верный."""
    with caplog.at_level(logging.INFO, logger="raketa"):
        fresh = TestClient(client.app)
        fresh.post(
            "/api/v1/auth/login",
            json={"email": EDITOR[0], "password": "не-тот-пароль-12345"},
        )
        text = _records(caplog)

    assert "не-тот-пароль-12345" not in text
    assert "неудачный вход" in text
    assert mask_email(EDITOR[0]) in text
    assert EDITOR[0] not in text


def test_successful_login_is_logged_by_identifier(client, caplog):
    with caplog.at_level(logging.INFO, logger="raketa"):
        fresh = TestClient(client.app)
        answer = fresh.post("/api/v1/auth/login", json={"email": EDITOR[0], "password": EDITOR[1]})
        assert answer.status_code == 200
        line = next(r for r in caplog.records if r.getMessage() == "вход")

    assert line.fields["user"] == answer.json()["id"]
    assert line.fields["role"] == "editor"
    assert EDITOR[0] not in _records(caplog)


def test_unhandled_error_is_logged_with_path_and_number(caplog):
    """Трассировка без пути и номера запроса — половина ответа на вопрос «что упало»."""
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.get("/boom")
    def boom() -> dict:
        raise RuntimeError("так не бывает")

    with caplog.at_level(logging.ERROR, logger="raketa"):
        with TestClient(app, raise_server_exceptions=False) as broken:
            broken.get("/boom")
        line = next(r for r in caplog.records if r.getMessage() == "запрос упал")

    assert line.fields["path"] == "/boom"
    assert line.request_id != "-"
    assert line.exc_info is not None, "без трассировки запись бесполезна"
    assert "так не бывает" in line.exc_text


def test_json_format_is_one_object_per_line():
    record = logging.LogRecord("raketa", logging.INFO, __file__, 1, "событие", None, None)
    record.request_id = "abc123"
    record.actor = "3/editor"
    record.fields = {"store": "store01", "facts": 12}

    line = json.loads(JsonFormatter().format(record))

    assert line["message"] == "событие"
    assert line["request_id"] == "abc123"
    assert line["actor"] == "3/editor"
    assert line["store"] == "store01" and line["facts"] == 12


def test_readiness_is_green_on_a_healthy_system(client, anon_client):
    answer = anon_client.get(READY)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["sync"] == "ok"


def test_failed_sync_turns_readiness_red(client, anon_client, failed_run):
    """Сбой загрузки должен быть виден снаружи, без входа в систему."""
    answer = anon_client.get(READY)

    assert answer.status_code == 503, answer.text
    assert answer.json()["status"] == "degraded"
    assert answer.json()["checks"]["sync"] == "failed"
    assert answer.json()["checks"]["stores_behind"] == "1"
    # Ни названий кабинетов, ни чисел: ручка открыта без входа.
    assert "store" not in json.dumps(answer.json()["checks"], ensure_ascii=False).replace(
        "stores_behind", ""
    )


def test_liveness_does_not_depend_on_the_data(anon_client, failed_run):
    """По живости перезапускают процесс: сбой загрузки — не повод его убивать."""
    answer = anon_client.get(HEALTH)

    assert answer.status_code == 200
    assert answer.json()["status"] == "ok"


def test_sync_status_names_the_cabinets_for_the_screen(client, failed_run):
    answer = client.get(STATUS)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["status"] == "degraded"
    assert body["stale_after_hours"] > 0
    broken = next(row for row in body["stores"] if row["store_id"] == failed_run)
    assert broken["status"] == "failed"
    assert broken["message"] == "RuntimeError: проверка"
    assert broken["store_name"], "названия нужны: это ответ человеку, а не мониторингу"


def test_old_success_counts_as_behind(client):
    """Удачная загрузка позавчера — тоже отставание: числа на экране уже не те."""
    from app.services.monitoring import STALE, sync_health

    created = client.post(
        "/api/v1/catalog/stores", json={"code": "g4-old", "name": "Отставший кабинет"}
    )
    assert created.status_code == 201, created.text
    store_id = created.json()["id"]
    with SessionLocal() as session:
        run = SyncRun(
            store_id=store_id,
            status=SyncStatus.SUCCESS,
            finished_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=3),
        )
        session.add(run)
        session.commit()
        run_id = run.id
    try:
        with SessionLocal() as session:
            health = sync_health(session)
        row = next(r for r in health.stores if r.store_id == store_id)
        assert row.status == STALE
        assert health.status == "degraded"
    finally:
        with SessionLocal() as session:
            session.delete(session.get(SyncRun, run_id))
            session.commit()
        client.delete(f"/api/v1/catalog/stores/{store_id}")


def test_cabinet_without_runs_does_not_cry_wolf(client):
    """На свежем развёртывании кабинет заводят раньше первой загрузки."""
    from app.services.monitoring import NEVER, sync_health

    created = client.post(
        "/api/v1/catalog/stores", json={"code": "g4-new", "name": "Только что заведён"}
    )
    assert created.status_code == 201, created.text
    store_id = created.json()["id"]
    try:
        with SessionLocal() as session:
            health = sync_health(session)
        row = next(r for r in health.stores if r.store_id == store_id)
        assert row.status == NEVER
        assert health.status == "ok", "«ни разу не загружался» — не сбой"
    finally:
        client.delete(f"/api/v1/catalog/stores/{store_id}")


def test_sync_status_is_closed_to_strangers(anon_client, viewer_client):
    assert anon_client.get(STATUS).status_code == 401
    assert viewer_client.get(STATUS).status_code == 200
