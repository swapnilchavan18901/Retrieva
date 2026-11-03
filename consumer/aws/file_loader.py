"""Download and extract PDFs with bounded network, disk, and text usage."""

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

import boto3
from botocore.config import Config
from pypdf import PdfReader

from consumer.consumer_env import env
from shared.document_limits import MAX_EXTRACTED_CHARACTERS, MAX_FILE_BYTES, MAX_PDF_PAGES


@dataclass
class Document:
    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)


s3 = boto3.client(
    "s3",
    region_name=env.AWS_REGION,
    aws_access_key_id=env.AWS_ACCESS_KEY_ID,
    aws_secret_access_key=env.AWS_SECRET_ACCESS_KEY,
    aws_session_token=env.AWS_SESSION_TOKEN,
    endpoint_url=f"https://s3.{env.AWS_REGION}.amazonaws.com",
    config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=30, retries={"max_attempts": 3}),
)
BUCKET = env.AWS_S3_BUCKET


def _load_pdf_from_s3_sync(key: str) -> tuple[list[Document], str]:
    response = s3.get_object(Bucket=BUCKET, Key=key)
    stream = response["Body"]
    tmp_path = None
    try:
        if not 0 < response["ContentLength"] <= MAX_FILE_BYTES:
            raise ValueError("PDF must be between 1 byte and 50 MiB")
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temporary:
            tmp_path = temporary.name
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError("PDF exceeds 50 MiB")
                temporary.write(chunk)
        if size != response["ContentLength"]:
            raise ValueError("Incomplete PDF download")
        # Keep the reader's file descriptor scoped, including parsing failures.
        with open(tmp_path, "rb") as pdf_file:
            if not pdf_file.read(1024).lstrip().startswith(b"%PDF-"):
                raise ValueError("Invalid PDF header")
            pdf_file.seek(0)
            reader = PdfReader(pdf_file)
            if reader.is_encrypted:
                raise ValueError("Encrypted PDFs are not supported")
            total_pages = len(reader.pages)
            if not 0 < total_pages <= MAX_PDF_PAGES:
                raise ValueError(f"PDF must contain between 1 and {MAX_PDF_PAGES} pages")
            docs, characters = [], 0
            for page_num, page in enumerate(reader.pages, start=1):
                text = page.extract_text(extraction_mode="layout") or ""
                characters += len(text)
                if characters > MAX_EXTRACTED_CHARACTERS:
                    raise ValueError("PDF contains too much extracted text")
                docs.append(Document(text, {"page": page_num, "source": key, "total_pages": total_pages}))
            if not any(doc.page_content.strip() for doc in docs):
                raise ValueError("PDF has no extractable text; scanned documents need OCR")
        return docs, tmp_path
    except BaseException:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    finally:
        stream.close()


async def load_pdf_from_s3(key: str) -> tuple[list[Document], str]:
    return await asyncio.to_thread(_load_pdf_from_s3_sync, key)
