from pydantic import BaseModel, Field, field_validator
from shared.document_limits import MAX_FILE_BYTES


class CreateUploadUrlRequest(BaseModel):
    fileName: str = Field(min_length=1, max_length=255)
    fileType: str = Field(min_length=1, max_length=100)
    fileSize: int = Field(gt=0, le=MAX_FILE_BYTES, strict=True)
    documentId: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("fileName")
    @classmethod
    def validate_filename(cls, value):
        if not value.strip() or any(char in value for char in ("/", "\\")) or any(ord(char) < 32 for char in value):
            raise ValueError("Use a plain file name without paths or control characters")
        return value


class CompleteUploadRequest(BaseModel):
    uploadId: str | None = Field(default=None, min_length=1, max_length=100)
