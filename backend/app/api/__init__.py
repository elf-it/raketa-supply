from fastapi import APIRouter, Depends

from . import auth, catalog, forecast, health, matrix, orders, purchase, reference, sync
from .deps import require_viewer

api_router = APIRouter(prefix="/api/v1")

# Вход открыт, остальное — только вошедшим. Права на изменение данных
# проверяются уже в самих маршрутах.
api_router.include_router(auth.router)
# Живость и готовность тоже открыты: мониторинг, который их опрашивает, сессии
# не заводит. Названий и чисел в их ответах поэтому нет.
api_router.include_router(health.router)

protected = APIRouter(dependencies=[Depends(require_viewer)])
protected.include_router(matrix.router)
protected.include_router(catalog.router)
protected.include_router(reference.router)
protected.include_router(forecast.router)
protected.include_router(purchase.router)
protected.include_router(orders.router)
protected.include_router(sync.router)
api_router.include_router(protected)

__all__ = ["api_router"]
