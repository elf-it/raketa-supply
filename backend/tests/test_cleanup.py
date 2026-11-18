"""Чистка журналов (задача G-19).

Главное здесь — не то, что удаляется, а то, что не удаляется. Последний
завершённый прогон кабинета держит проверку свежести данных (G-4): удалив
его, чистка молча погасила бы алерт о протухших числах. И журнал
корректировок — доказательство, кто поставил число; по умолчанию его
не трогают вовсе.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models import (
    AdjustmentLog,
    RevokedSession,
    Store,
    StoreProduct,
    SyncRun,
    SyncStatus,
)
from app.services.cleanup import plan_cleanup, protected_run_ids, run_cleanup
from app.services.monitoring import NEVER, sync_health


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _add_runs(store_id: int, ages_days: list[int]) -> list[int]:
    """Прогоны заданного возраста. Возвращает их id."""
    ids: list[int] = []
    with SessionLocal() as session:
        for age in ages_days:
            moment = _now() - timedelta(days=age)
            run = SyncRun(
                store_id=store_id,
                status=SyncStatus.SUCCESS,
                started_at=moment,
                finished_at=moment,
                facts_upserted=1,
            )
            session.add(run)
            session.flush()
            ids.append(run.id)
        session.commit()
    return ids


def _forget(run_ids: list[int], log_ids: list[int] | None = None) -> None:
    with SessionLocal() as session:
        if run_ids:
            session.execute(delete(SyncRun).where(SyncRun.id.in_(run_ids)))
        if log_ids:
            session.execute(delete(AdjustmentLog).where(AdjustmentLog.id.in_(log_ids)))
        session.commit()


def _store_id() -> int:
    with SessionLocal() as session:
        return session.scalars(select(Store.id).order_by(Store.code)).first()


def test_old_runs_go_and_fresh_ones_stay(client):
    store_id = _store_id()
    old, older, fresh = _add_runs(store_id, [100, 200, 2])
    try:
        with SessionLocal() as session:
            plan = plan_cleanup(session, run_days=90)
            assert plan.runs >= 2

            run_cleanup(session, run_days=90)

        with SessionLocal() as session:
            alive = set(session.scalars(select(SyncRun.id)).all())
        assert old not in alive and older not in alive
        assert fresh in alive
    finally:
        _forget([old, older, fresh])


def test_the_last_finished_run_is_never_deleted(client):
    """Иначе кабинет выглядит «ни разу не загружался», и алерт замолкает."""
    store_id = _store_id()
    # Единственный прогон кабинета — и он старше любого срока хранения.
    with SessionLocal() as session:
        session.execute(delete(SyncRun).where(SyncRun.store_id == store_id))
        session.commit()
    (ancient,) = _add_runs(store_id, [999])
    try:
        with SessionLocal() as session:
            assert ancient in protected_run_ids(session).values()
            run_cleanup(session, run_days=1)

        with SessionLocal() as session:
            assert session.get(SyncRun, ancient) is not None
            health = sync_health(session)
        row = next(r for r in health.stores if r.store_id == store_id)
        assert row.status != NEVER, "кабинет не должен превратиться в «ни разу не грузился»"
    finally:
        _forget([ancient])


def test_the_journal_is_left_alone_by_default(client):
    """Журнал корректировок — доказательство, а не мусор."""
    with SessionLocal() as session:
        pair_id = session.scalars(select(StoreProduct.id).order_by(StoreProduct.id)).first()
        entry = AdjustmentLog(
            store_product_id=pair_id,
            week="2020-W01",
            field="manual_forecast",
            operation="manual",
            old_value=1,
            new_value=2,
            author="тест",
            created_at=_now() - timedelta(days=2000),
        )
        session.add(entry)
        session.commit()
        log_id = entry.id
    try:
        with SessionLocal() as session:
            plan = run_cleanup(session, run_days=90)
            assert plan.journal == 0
            assert session.get(AdjustmentLog, log_id) is not None

        # И только с явным ключом — удаляется.
        with SessionLocal() as session:
            plan = run_cleanup(session, run_days=0, journal_days=365)
            assert plan.journal >= 1
            assert session.get(AdjustmentLog, log_id) is None
    finally:
        _forget([], [log_id])


def test_zero_days_means_do_not_touch(client):
    store_id = _store_id()
    (old,) = _add_runs(store_id, [500])
    try:
        with SessionLocal() as session:
            before = session.scalar(select(func.count()).select_from(SyncRun))
            plan = run_cleanup(session, run_days=0, journal_days=0)
            after = session.scalar(select(func.count()).select_from(SyncRun))

        assert plan.total == 0
        assert before == after
    finally:
        _forget([old])


def test_a_dry_run_changes_nothing(client):
    store_id = _store_id()
    old, older = _add_runs(store_id, [300, 400])
    try:
        with SessionLocal() as session:
            plan = plan_cleanup(session, run_days=90)
            # Из двух древних прогонов один — последний завершённый у кабинета,
            # и он неприкосновенен; к удалению идёт только второй.
            assert plan.runs == 1
            assert old in protected_run_ids(session).values()

        with SessionLocal() as session:
            assert session.get(SyncRun, old) is not None
            assert session.get(SyncRun, older) is not None
    finally:
        _forget([old, older])


def test_expired_revocations_go_without_a_key_and_without_a_retention(seeded_db):
    """Отозванная сессия (G-20) хранится ровно до срока своего токена.

    Срока хранения у этих строк нет и быть не может: держать дольше
    бессмысленно — просроченный токен отвергает подпись, — а удалять раньше
    значит вернуть отозванный токен в работу. Поэтому чистка берёт их
    при любых ключах, в том числе когда журналы не чистятся вовсе.
    """
    now = _now()
    with SessionLocal() as session:
        try:
            session.add_all(
                [
                    RevokedSession(jti="просроченный", expires_at=now - timedelta(minutes=1)),
                    RevokedSession(jti="действующий", expires_at=now + timedelta(hours=5)),
                ]
            )
            session.commit()

            plan = plan_cleanup(session, run_days=0, journal_days=0, now=now)
            assert plan.sessions == 1

            done = run_cleanup(session, run_days=0, journal_days=0, now=now)

            # Проверяются свои две строки, а не вся таблица: выход вызывают
            # и другие тесты, и их отзывы — такие же законные строки.
            assert done.sessions == 1
            assert session.get(RevokedSession, "просроченный") is None
            assert session.get(RevokedSession, "действующий") is not None
        finally:
            session.execute(
                delete(RevokedSession).where(
                    RevokedSession.jti.in_(["просроченный", "действующий"])
                )
            )
            session.commit()


def test_the_command_says_what_it_kept(client, capsys):
    from app import cleanup as cli

    store_id = _store_id()
    (old,) = _add_runs(store_id, [365])
    try:
        assert cli.main(dry_run=True) == cli.OK
        out = capsys.readouterr().out

        assert "к удалению" in out
        assert "Последний завершённый прогон сохранён" in out
        assert "не трогался" in out, "о журнале нужно сказать, а не умолчать"
        assert "Ничего не удалено" in out
    finally:
        _forget([old])
