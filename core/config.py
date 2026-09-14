from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EQUITY_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://equity:equity_local_dev@127.0.0.1:5433/equity"
    test_database_url: str = (
        "postgresql+psycopg://equity:equity_local_dev@127.0.0.1:5433/equity_test"
    )


def get_settings() -> Settings:
    return Settings()
