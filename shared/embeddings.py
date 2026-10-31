"""OpenAI text embeddings billed through OpenRouter, shared by ingestion and search."""

import asyncio
import math
from time import perf_counter
from shared.benchmark import record_provider_call
from functools import lru_cache
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

EMBEDDING_MODEL = "openai/text-embedding-3-small"
VECTOR_SIZE = 1536
EMBEDDING_URL = "https://openrouter.ai/api/v1/embeddings"


class EmbeddingSettings(BaseSettings):
    OPENROUTER_API_KEY: str
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
        extra="ignore",
        case_sensitive=True,
    )


@lru_cache(maxsize=1)
def _api_key() -> str:
    key = EmbeddingSettings().OPENROUTER_API_KEY.strip()
    if not key:
        raise ValueError("Set OPENROUTER_API_KEY in Retrieva/.env to enable embeddings")
    return key


def collection_for_bot(bot_id: str) -> str:
    """Reuse original 1536-dimensional text-embedding-3-small collections."""
    return bot_id


def _normalized_vectors(data: dict, expected_count: int) -> list[list[float]]:
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or len(items) != expected_count:
        raise ValueError("OpenRouter returned an unexpected number of embeddings")
    indices = [item.get("index") if isinstance(item, dict) else None for item in items]
    if any(type(index) is not int for index in indices) or sorted(indices) != list(range(expected_count)):
        raise ValueError("OpenRouter returned invalid embedding indices")
    vectors = []
    for item in sorted(items, key=lambda item: item["index"]):
        values = item.get("embedding")
        if not isinstance(values, list) or len(values) != VECTOR_SIZE:
            raise ValueError("OpenRouter returned an unexpected embedding shape")
        if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise ValueError("OpenRouter returned invalid embedding values")
        norm = math.sqrt(sum(value * value for value in values))
        if not norm or not math.isfinite(norm):
            raise ValueError("OpenRouter returned an invalid embedding norm")
        vectors.append([value / norm for value in values])
    return vectors


async def embed_texts(
    texts: list[str],
    batch_size: int = 32,
) -> list[list[float]]:
    """Embed using OpenRouter credits; no direct OpenAI key or model download."""
    if not 1 <= batch_size <= 100:
        raise ValueError("Embedding batch size must be between 1 and 100")
    if not texts:
        return []
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("Embedding inputs must be non-empty strings")

    vectors = []
    async with httpx.AsyncClient(timeout=60) as client:
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset:offset + batch_size]
            body = {
                "model": EMBEDDING_MODEL,
                "input": batch,
                "dimensions": VECTOR_SIZE,
                "encoding_format": "float",
            }
            for attempt in range(3):
                started = perf_counter()
                response, data = None, {}
                retry = False
                try:
                    response = await client.post(
                        EMBEDDING_URL, headers={"Authorization": f"Bearer {_api_key()}"}, json=body,
                    )
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        record_provider_call(provider="openrouter", operation="embedding", model=EMBEDDING_MODEL,
                            duration_ms=(perf_counter() - started) * 1000, data=data, status="retryable_error",
                            http_status=response.status_code, attempt=attempt + 1, batch_size=len(batch),
                            provider_request_id=response.headers.get("x-request-id"))
                        retry = True
                    else:
                        response.raise_for_status()
                        batch_vectors = _normalized_vectors(data, len(batch))
                except BaseException as error:
                    record_provider_call(provider="openrouter", operation="embedding", model=EMBEDDING_MODEL,
                        duration_ms=(perf_counter() - started) * 1000, data=data,
                        status="cancelled" if type(error).__name__ == "CancelledError" else "error",
                        error_type=type(error).__name__, http_status=response.status_code if response is not None else None,
                        attempt=attempt + 1, batch_size=len(batch),
                        provider_request_id=response.headers.get("x-request-id") if response is not None else None)
                    raise
                if retry:
                    await asyncio.sleep(2 ** attempt)
                    continue
                record_provider_call(provider="openrouter", operation="embedding", model=EMBEDDING_MODEL,
                    duration_ms=(perf_counter() - started) * 1000, data=data, http_status=response.status_code,
                    attempt=attempt + 1, batch_size=len(batch), provider_request_id=response.headers.get("x-request-id"))
                vectors.extend(batch_vectors)
                break

    return vectors
