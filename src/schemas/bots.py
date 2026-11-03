from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints, field_validator

BotName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=100)]

class BotCreateRequest(BaseModel):
    name: BotName
    description: str | None = Field(default=None, max_length=2000)
    systemPrompt: str | None = Field(default=None, max_length=16_000)


class BotUpdateRequest(BaseModel):
    name: BotName | None = None
    description: str | None = Field(default=None, max_length=2000)
    systemPrompt: str | None = Field(default=None, max_length=16_000)
    isActive: bool | None = None

    @field_validator("name", "isActive")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("This field cannot be null; omit it to keep the current value")
        return value
