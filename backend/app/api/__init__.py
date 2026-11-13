from fastapi import APIRouter

from . import catalog, forecast, matrix, purchase, reference, sync

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(matrix.router)
api_router.include_router(catalog.router)
api_router.include_router(reference.router)
api_router.include_router(forecast.router)
api_router.include_router(purchase.router)
api_router.include_router(sync.router)

__all__ = ["api_router"]
