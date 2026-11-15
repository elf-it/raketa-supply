from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Ракета · Веб-система снабжения"
    #: SQLite по умолчанию, чтобы проект поднимался без Docker.
    #: В проде — postgresql+psycopg://user:pass@host:5432/raketa
    database_url: str = "sqlite:///./raketa.db"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    #: Источник данных WB: ``fixtures`` (локальные файлы) или ``http`` (реальный API).
    wb_client: str = "fixtures"
    wb_api_base: str = "https://statistics-api.wildberries.ru"
    #: Ключи кабинетов задаются только через окружение, в базе не хранятся:
    #: WB_TOKENS="store01=eyJ...,store02=eyJ..."
    wb_tokens: str = ""
    debug: bool = True

    #: Ключ подписи сессионных токенов. В проде задаётся только через окружение.
    secret_key: str = "dev-only-insecure-key-change-me"
    session_ttl_hours: int = 12
    #: Кука только по HTTPS. В разработке выключено, иначе вход не работает по http.
    cookie_secure: bool = False

    #: Ограничение перебора паролей: сколько неудачных попыток подряд допускается
    #: с одного адреса для одной почты и сколько — с адреса на все учётные записи.
    #: За обратным прокси без проброса адреса второй порог придётся поднять:
    #: там весь офис выглядит одним клиентом. Подробности — app/ratelimit.py.
    login_max_attempts: int = 10
    login_max_attempts_per_address: int = 60
    login_window_minutes: int = 15

    #: Сколько доверенных прокси стоит перед приложением (задача G-12).
    #: 0 — приложение видит клиента напрямую, и ``X-Forwarded-For`` не читается:
    #: без прокси этот заголовок подставляет сам клиент, и счётчик по адресу
    #: обходился бы одной строкой в запросе. За одним nginx — 1.
    trusted_proxy_hops: int = 0

    #: Кэш недельных рядов: сколько записей «пара × ось недель» держать в памяти
    #: процесса. Запись — около 0,2 КБ на неделю оси, то есть 8 КБ на замеренных
    #: осях матрицы и закупки; предел по умолчанию — это потолок примерно
    #: в 80 МБ на воркер, а занято будет ровно по каталогу. Каждая пара занимает
    #: по записи на экран: 5000 пар — это 10 000 записей.
    #: Предел меньше каталога бесполезен: запрос обходит все пары подряд
    #: и вытесняет то, что сам же положил. 0 выключает кэш — расчёт идёт
    #: на каждый запрос, как до задачи G-13.
    #: Подробности — app/services/calc_cache.py и docs/08-performance.md.
    calc_cache_entries: int = 10_000

    #: Логи (задача G-4). ``text`` — человеку в терминал, ``json`` — сборщику
    #: логов: по строке-объекту на событие. Уровень ``DEBUG`` добавляет к строке
    #: запроса ещё и SQL, если его включит сам SQLAlchemy.
    log_level: str = "INFO"
    log_format: str = "text"

    #: Через сколько часов без удачной загрузки кабинет считается отставшим.
    #: Сутки с запасом: суточная загрузка, запущенная вчера утром и не
    #: запущенная сегодня, должна успеть попасть в «устарело» до того,
    #: как по этим числам соберут заказ.
    sync_stale_hours: int = 36

    def validate_runtime(self) -> None:
        """Проверки, после которых приложению можно давать работать.

        Токен, подписанный общеизвестным ключом, подделывается кем угодно, поэтому
        с ключом по умолчанию вне отладки стартовать нельзя.
        """
        if not self.debug and self.secret_key == Settings.model_fields["secret_key"].default:
            raise RuntimeError(
                "SECRET_KEY не задан. Сгенерируйте ключ "
                '(`python -c "import secrets; print(secrets.token_urlsafe(48))"`) '
                "и передайте его через окружение."
            )
        if not self.debug and not self.cookie_secure:
            raise RuntimeError(
                "COOKIE_SECURE=false вне отладки: сессионная кука уйдёт по открытому HTTP."
            )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def wb_token_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for item in self.wb_tokens.split(","):
            if "=" in item:
                code, token = item.split("=", 1)
                out[code.strip()] = token.strip()
        return out


@lru_cache
def get_settings() -> Settings:
    return Settings()
