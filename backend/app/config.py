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
