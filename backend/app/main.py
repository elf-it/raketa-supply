from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import api_router
from .config import get_settings
from .db import engine
from .migrations import assert_current
from .observability import RequestLogMiddleware, configure_logging, event


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Приложение не создаёт и не правит схему: это делает явный шаг развёртывания.
    # Работать на устаревшей базе хуже, чем не запуститься.
    settings = get_settings()
    settings.validate_runtime()
    assert_current(engine)
    # Первая строка в логе отвечает на вопросы, которые иначе задают человеку:
    # какая версия поднялась, в каком режиме и на какой базе. Пароль из URL
    # базы убран: логи читают шире, чем настройки.
    event(
        "приложение запущено",
        version=__version__,
        debug=settings.debug,
        database=engine.url.render_as_string(hide_password=True),
        wb_client=settings.wb_client,
    )
    yield
    event("приложение остановлено", version=__version__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Прогнозирование продаж, остатки и планирование закупок. "
            "Формулы расчётного ядра — docs/03-calc-spec.md."
        ),
        lifespan=lifespan,
    )
    # allow_credentials нужен для сессионной куки, поэтому список источников
    # обязан быть конкретным: со звёздочкой браузер куку не отправит.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Номер запроса виден браузеру: по нему человек находит свой запрос в логе.
        expose_headers=["X-Request-Id"],
    )
    app.add_middleware(RequestLogMiddleware)
    app.include_router(api_router)
    return app


app = create_app()
