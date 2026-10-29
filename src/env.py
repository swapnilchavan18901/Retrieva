from pydantic_settings import BaseSettings
from pathlib import Path

class Env(BaseSettings):
    DATABASE_URL: str
    JWT_SECRET: str

    # optional defaults
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440
    AWS_REGION: str
    AWS_S3_BUCKET: str
    AWS_ACCESS_KEY_ID: str
    AWS_SECRET_ACCESS_KEY: str
    RABBIT_MQ_URL: str
    RABBIT_MQ_API_KEY: str
    SARVAM_API_KEY: str
    OPENROUTER_API_KEY: str
    SARVAM_BASE_URL: str = "https://api.sarvam.ai/v2"
    SARVAM_MODEL: str = "sarvam-105b"
    QDRANT_DB_CLUSTER_URL:str
    QDRANT_DB_API_KEY:str
    class Config:
        env_file = Path(__file__).resolve().parents[1] / ".env"
        case_sensitive = True
        extra = "ignore"  # Allow obsolete keys and consumer-only settings.

env = Env()
