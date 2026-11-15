"""Загрузка по расписанию одной командой (задача E-6).

Главные проверки — те, ради которых команда вообще отличается от кнопки
на экране: две загрузки разом не идут, брошенный прогон не останавливает
расписание навсегда, а код возврата говорит планировщику, что случилось,
— иначе cron будет молчать и при удаче, и при сбое одинаково.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app import sync as sync_cli
from app.db import SessionLocal
from app.models import Store, SyncRun, SyncStatus


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _runs() -> list[SyncRun]:
    with SessionLocal() as session:
        return list(session.scalars(select(SyncRun).order_by(SyncRun.id)).all())


def _drop_runs(after_id: int) -> None:
    with SessionLocal() as session:
        for run in session.scalars(select(SyncRun).where(SyncRun.id > after_id)).all():
            session.delete(run)
        session.commit()


def _last_id() -> int:
    runs = _runs()
    return runs[-1].id if runs else 0


def test_command_loads_every_cabinet_and_reports(client, capsys):
    """client — чтобы база была развёрнута сидом, как в проде после миграций."""
    mark = _last_id()
    try:
        code = sync_cli.main(days=30)
        out = capsys.readouterr().out

        assert code == sync_cli.OK
        with SessionLocal() as session:
            stores = session.scalars(select(Store).where(Store.is_active.is_(True))).all()
        assert len(stores) > 0
        for store in stores:
            assert store.code in out, "в отчёте должен быть каждый кабинет"
        fresh = [run for run in _runs() if run.id > mark]
        assert len(fresh) == len(stores)
        assert all(run.status is SyncStatus.SUCCESS for run in fresh)
        assert all(run.finished_at is not None for run in fresh)
    finally:
        _drop_runs(mark)


def test_second_run_does_not_start_while_the_first_is_going(client, capsys):
    """Два прогона по одному кабинету пишут одни и те же факты и мешают друг другу."""
    mark = _last_id()
    with SessionLocal() as session:
        session.add(SyncRun(store_id=None, status=SyncStatus.RUNNING, started_at=_now()))
        session.commit()
    try:
        code = sync_cli.main(days=30)
        out = capsys.readouterr().out

        assert code == sync_cli.BUSY
        assert "ещё идёт" in out
        fresh = [run.id for run in _runs() if run.id > mark]
        assert len(fresh) == 1, "кроме подложенного прогона новых не появилось"
    finally:
        _drop_runs(mark)


def test_abandoned_run_does_not_stop_the_schedule(client, capsys):
    """Процесс убили на середине — расписание должно поехать дальше."""
    mark = _last_id()
    with SessionLocal() as session:
        session.add(
            SyncRun(
                store_id=None,
                status=SyncStatus.RUNNING,
                started_at=_now() - timedelta(hours=2),
            )
        )
        session.commit()
    try:
        code = sync_cli.main(days=30)
        out = capsys.readouterr().out

        assert code == sync_cli.OK
        assert "Закрыто брошенных прогонов: 1" in out
        abandoned = [run for run in _runs() if run.id > mark][0]
        assert abandoned.status is SyncStatus.FAILED
        assert abandoned.finished_at is not None, "висеть в журнале вечно он не должен"
        assert "не завершился" in (abandoned.message or "")
    finally:
        _drop_runs(mark)


def test_failed_cabinet_gives_a_non_zero_code(client, capsys, monkeypatch):
    """По коду возврата планировщик и поймёт, что пора писать письмо."""
    mark = _last_id()

    class Broken:
        def fetch_sales(self, store_code, date_from):
            raise RuntimeError("401 Unauthorized")

        def fetch_stocks(self, store_code, date_from):
            raise RuntimeError("401 Unauthorized")

        def fetch_cards(self, store_code):
            return []

    monkeypatch.setattr(sync_cli, "make_client", lambda *args, **kwargs: Broken())
    try:
        code = sync_cli.main(days=7)
        err = capsys.readouterr().err

        assert code == sync_cli.FAILED
        assert "401 Unauthorized" in err, "причина — в stderr, его и пришлёт cron"
        fresh = [run for run in _runs() if run.id > mark]
        assert fresh and all(run.status is SyncStatus.FAILED for run in fresh)
    finally:
        _drop_runs(mark)


def test_depth_in_days_reaches_the_client(client, monkeypatch):
    """Ежедневному прогону хватает двух недель, и это должно доезжать до запроса."""
    seen: dict[str, object] = {}

    def fake_sync_all(session, client_, days=90, today=None):
        seen["days"] = days
        return []

    monkeypatch.setattr(sync_cli, "sync_all", fake_sync_all)

    code = sync_cli.main(days=14)

    assert code == sync_cli.OK
    assert seen["days"] == 14


def test_unfinished_run_is_judged_by_age(client):
    """Граница «идёт / брошен» — возраст прогона, а не его статус."""
    mark = _last_id()
    moment = _now()
    with SessionLocal() as session:
        session.add(SyncRun(status=SyncStatus.RUNNING, started_at=moment - timedelta(minutes=10)))
        session.commit()
    try:
        with SessionLocal() as session:
            assert sync_cli.unfinished_run(session, lock_minutes=30, now=moment) is not None
            assert sync_cli.unfinished_run(session, lock_minutes=5, now=moment) is None
    finally:
        _drop_runs(mark)
