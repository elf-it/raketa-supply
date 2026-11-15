"""Живость и готовность (задача G-4).

Две разные проверки, и путать их дорого:

* **Живость** отвечает «процесс поднялся». Она ничего не опрашивает — по ней
  перезапускают зависший процесс, и проверка, зависящая от базы, устроила бы
  перезапуск всего приложения из-за недоступной на минуту базы.
* **Готовность** отвечает «системе можно верить»: база отвечает, данные
  кабинетов свежие. Ею меряет внешний мониторинг, и она же единственный способ
  узнать о сбое загрузки, не заходя в интерфейс: почты и мессенджера у системы
  нет (вопрос Q-16).

Обе открыты без входа: проверяющий их мониторинг сессии не заводит. Поэтому
в ответе нет ни названий кабинетов, ни чисел — только состояние. Подробности,
вместе с названиями, отдаёт `/sync/status`, и она закрыта ролью.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi import status as http_status
from sqlalchemy import text
from sqlalchemy.orm import Session

from .. import __version__
from ..db import get_session
from ..services.monitoring import BAD, sync_health

router = APIRouter(prefix="/health", tags=["Служебное"])


@router.get("", summary="Проверка живости")
def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/ready", summary="Готовность к работе")
def ready(response: Response, session: Session = Depends(get_session)) -> dict:
    """503, если база недоступна или данные кабинетов отстали.

    Код ответа, а не поле в теле: мониторинг, который умеет только «200 или
    нет», — это любой мониторинг, а разбирать тело умеет не каждый.
    """
    checks: dict[str, str] = {}
    try:
        session.execute(text("select 1"))
        checks["database"] = "ok"
    except Exception as error:  # noqa: BLE001 — любая ошибка базы означает «не готов»
        checks["database"] = f"error: {type(error).__name__}"

    try:
        health_report = sync_health(session)
        bad = [row for row in health_report.stores if row.status in BAD]
        checks["sync"] = bad[0].status if bad else "ok"
        checks["stores_behind"] = str(len(bad))
    except Exception as error:  # noqa: BLE001
        checks["sync"] = f"error: {type(error).__name__}"

    ready_now = (
        all(not value.startswith("error") for value in checks.values())
        and checks.get("sync") not in BAD
    )
    if not ready_now:
        response.status_code = http_status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ready_now else "degraded", "version": __version__, "checks": checks}
