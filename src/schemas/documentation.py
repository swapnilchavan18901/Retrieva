from pydantic import BaseModel, Field


class CreateUploadUrlRequest(BaseModel):
    fileName: str
    fileType: str
    fileSize: int = Field(gt=0, le=50 * 1024 * 1024)
    documentId: str | None = None


class CompleteUploadRequest(BaseModel):
    uploadId: str | None = None
