from pathlib import Path

from pydantic_settings import SettingsConfigDict

from shared.settings import ServiceSettings


class Env(ServiceSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[1] / ".env")


env = Env()
