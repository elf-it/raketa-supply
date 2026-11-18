"""Чистка журналов одной командой — для планировщика (задача G-19).

    python -m app.cleanup --dry-run          — показать, что удалится
    python -m app.cleanup                    — удалить прогоны старше 90 дней
    python -m app.cleanup --journal-days 365 — заодно и журнал правок

Решения и почему так:

* **Прогон вхолостую идёт первым.** Удаление в базе не отменяется кнопкой,
  поэтому ``--dry-run`` показывает те же числа, что посчитает настоящая
  чистка, и ничего не трогает.

* **Журнал корректировок по умолчанию не чистится.** Срок хранения
  доказательства назначает тот, кто отвечает за данные; программа при
  установке такого решения принять не может. Поэтому отдельный ключ
  и отдельный срок.

* **Последний завершённый прогон кабинета остаётся всегда** — на нём держится
  проверка свежести данных (задача G-4). Команда говорит, сколько таких строк
  сберегла, чтобы это не выглядело недоработкой.
"""

from __future__ import annotations

import argparse
import logging
import sys

from .db import SessionLocal, engine
from .migrations import assert_current
from .observability import configure_logging, event
from .services.cleanup import (
    DEFAULT_JOURNAL_DAYS,
    DEFAULT_RUN_DAYS,
    plan_cleanup,
    run_cleanup,
)

OK = 0
FAILED = 1


def main(
    run_days: int = DEFAULT_RUN_DAYS,
    journal_days: int = DEFAULT_JOURNAL_DAYS,
    dry_run: bool = False,
) -> int:
    from .config import get_settings

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    # Схему команда не правит: чистить журналы на устаревшей схеме —
    # хороший способ удалить не то.
    assert_current(engine)

    with SessionLocal() as session:
        plan = plan_cleanup(session, run_days, journal_days) if dry_run else None
        if plan is None:
            plan = run_cleanup(session, run_days, journal_days)

    if run_days > 0:
        print(
            f"Прогоны синхронизации старше {run_days} дн.: "
            f"{'к удалению' if dry_run else 'удалено'} {plan.runs}"
        )
        if plan.kept_runs:
            print(
                f"  Последний завершённый прогон сохранён у кабинетов: "
                f"{', '.join(plan.kept_runs)} — по нему считается свежесть данных."
            )
    else:
        print("Прогоны синхронизации не чистились: --run-days 0.")

    if journal_days > 0:
        print(
            f"Журнал корректировок старше {journal_days} дн.: "
            f"{'к удалению' if dry_run else 'удалено'} {plan.journal}"
        )
    else:
        print(
            "Журнал корректировок не трогался: это доказательство, кто и когда "
            "поставил число. Чистить его — отдельный ключ --journal-days."
        )

    if dry_run:
        print("Ничего не удалено: прогон вхолостую.")
    else:
        event(
            "чистка журналов",
            level=logging.INFO,
            runs=plan.runs,
            journal=plan.journal,
            run_days=run_days,
            journal_days=journal_days,
        )
    return OK


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Чистка журналов: прогоны синхронизации и журнал корректировок",
        epilog=(
            "Последний завершённый прогон кабинета не удаляется никогда: "
            "по нему считается свежесть данных."
        ),
    )
    parser.add_argument(
        "--run-days",
        type=int,
        default=DEFAULT_RUN_DAYS,
        help=(
            "сколько дней хранить прогоны синхронизации (0 — не чистить); "
            f"по умолчанию {DEFAULT_RUN_DAYS}"
        ),
    )
    parser.add_argument(
        "--journal-days",
        type=int,
        default=DEFAULT_JOURNAL_DAYS,
        help="сколько дней хранить журнал корректировок; 0 (по умолчанию) — не чистить",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать, что удалится, и ничего не удалять",
    )
    args = parser.parse_args()
    try:
        raise SystemExit(
            main(
                run_days=args.run_days,
                journal_days=args.journal_days,
                dry_run=args.dry_run,
            )
        )
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(FAILED) from None
