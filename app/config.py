from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    meta_verify_token: str = "change-me"
    meta_app_secret: str = ""
    wa_access_token: str = ""
    wa_phone_id: str = ""
    anthropic_api_key: str = ""
    session_secret: str = "change-me"
    modo_dev: bool = True
    turso_database_url: str = ""
    turso_auth_token: str = ""
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
