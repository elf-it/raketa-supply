from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import api_router
from .config import get_settings
from .db import engine
from .migrations import assert_current


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Приложение не создаёт и не правит схему: это делает явный шаг развёртывания.
    # Работать на устаревшей базе хуже, чем не запуститься.
    assert_current(engine)
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Прогнозирование продаж, остатки и планирование закупок. "
            "Формулы расчётного ядра — docs/03-calc-spec.md."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    @app.get("/api/v1/health", tags=["Служебное"], summary="Проверка живости")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
