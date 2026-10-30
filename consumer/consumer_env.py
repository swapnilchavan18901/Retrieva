from pydantic_settings import BaseSettings
from pathlib import Path
from pydantic import Field

class Env(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str

    # optional defaults
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
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

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        def environment_without_aws():
            # Match the API: do not inherit AWS credentials from the shell.
            return {key: value for key, value in env_settings().items() if not key.startswith("AWS_")}

        return init_settings, environment_without_aws, dotenv_settings, file_secret_settings

    class Config:
        env_file = Path(__file__).resolve().parents[1] / ".env"
        case_sensitive = True
        # The shared .env also contains API-only settings, including Sarvam.
        extra = "ignore"
        hide_input_in_errors = True

env = Env()
