"""Common API and worker settings, including explicit AWS source precedence."""

from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class ServiceSettings(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=1440, gt=0, le=43_200)
    AWS_REGION: str = Field(min_length=1)
    AWS_S3_BUCKET: str = Field(min_length=1)
    AWS_ACCESS_KEY_ID: str = Field(min_length=1)
    AWS_SECRET_ACCESS_KEY: str = Field(min_length=1)
    AWS_SESSION_TOKEN: str | None = None
    RABBIT_MQ_URL: str
    RABBIT_MQ_API_KEY: str
    OPENROUTER_API_KEY: str
    QDRANT_DB_CLUSTER_URL: str
    QDRANT_DB_API_KEY: str

    model_config = SettingsConfigDict(
        case_sensitive=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ):
        def without_aws(source: PydanticBaseSettingsSource) -> dict[str, Any]:
            return {key: value for key, value in source().items() if not key.startswith("AWS_")}

        # Keep AWS credentials and their optional session token from one source.
        # Non-AWS process settings retain their normal precedence over .env.
        return (
            init_settings,
            lambda: without_aws(env_settings),
            dotenv_settings,
            lambda: without_aws(file_secret_settings),
        )
