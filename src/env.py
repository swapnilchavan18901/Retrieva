from pathlib import Path

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import SettingsConfigDict

from shared.settings import ServiceSettings


class Env(ServiceSettings):
    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    HTTP_REQUESTS_PER_MINUTE: int = Field(default=120, ge=1, le=10_000)
    MAX_BOTS_PER_USER: int = Field(default=10, ge=1)
    MAX_DOCUMENTS_PER_BOT: int = Field(default=50, ge=1)
    MAX_API_KEYS_PER_BOT: int = Field(default=10, ge=1)
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    SOCKET_CORS_ORIGINS: list[str] = Field(default_factory=list)
    SARVAM_API_KEY: str
    SARVAM_BASE_URL: str = "https://api.sarvam.ai/v2"
    SARVAM_MODEL: str = "sarvam-105b"

    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env")

    @model_validator(mode="after")
    def validate_production_settings(self):
        if not self.SOCKET_CORS_ORIGINS:
            self.SOCKET_CORS_ORIGINS = self.CORS_ORIGINS.copy()
        if self.ENVIRONMENT == "production":
            if len(self.JWT_SECRET) < 32 or "replace-with" in self.JWT_SECRET.lower():
                raise ValueError("Production requires a random JWT_SECRET of at least 32 characters")
            for origins in (self.CORS_ORIGINS, self.SOCKET_CORS_ORIGINS):
                if not origins or any(not origin.startswith("https://") or "*" in origin for origin in origins):
                    raise ValueError("Production requires explicit HTTPS browser origins")
        return self


env = Env()
