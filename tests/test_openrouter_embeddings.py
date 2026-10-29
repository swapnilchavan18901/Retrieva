"""Offline integration checks with in-memory Qdrant and mocked OpenRouter HTTP."""

import importlib
import json
import sys
import types
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from shared import embeddings


class OpenRouterEmbeddingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = QdrantClient(":memory:")
        self.module_patch = patch.dict(sys.modules, {
            "consumer.vector.qdrantdb": types.SimpleNamespace(qdrant_client=self.db),
            "src.vector.client": types.SimpleNamespace(qdrant_client=self.db),
        })
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.addCleanup(self.db.close)
        self.schema = importlib.reload(importlib.import_module("consumer.vector.vector"))
        self.ingestion = importlib.reload(importlib.import_module("consumer.vector.insert"))
        self.retrieval = importlib.reload(importlib.import_module("src.vector.retrieve"))
        self.migration = importlib.reload(importlib.import_module("consumer.utils.reindex_embeddings"))
        self.calls = []
        self.statuses = []
        self.response_body = None
        original_client = httpx.AsyncClient

        def handle(request):
            self.calls.append(request)
            status = self.statuses.pop(0) if self.statuses else 200
            if status != 200:
                return httpx.Response(status, json={"error": "test error"})
            if self.response_body is not None:
                return httpx.Response(200, json=self.response_body)
            vectors = []
            for index, text in enumerate(json.loads(request.content)["input"]):
                vector = [0.0] * embeddings.VECTOR_SIZE
                vector[0 if "apple" in text else 1] = 2.0
                vectors.append({"index": index, "embedding": vector})
            return httpx.Response(200, json={"data": list(reversed(vectors))})

        def make_client(**kwargs):
            return original_client(transport=httpx.MockTransport(handle), **kwargs)

        self.http_patch = patch.object(embeddings.httpx, "AsyncClient", side_effect=make_client)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.key_patch = patch.object(embeddings, "_api_key", return_value="test-openrouter-key")
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)

    async def test_ingest_and_retrieve_use_same_model_and_collection(self):
        bot_id = str(uuid.uuid4())
        docs = [types.SimpleNamespace(page_content=text, metadata={"page":page})
                for page, text in enumerate(["apple information", "orange information"])]
        await self.ingestion.embed_and_store(docs, bot_id, "document", "fruit.pdf", "user")
        result = await self.retrieval.retrieve_relevant_chunks(bot_id, "apple question", top_k=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"].strip(), "apple information")
        self.assertEqual(result[0]["file_name"], "fruit.pdf")
        target = embeddings.collection_for_bot(bot_id)
        self.assertEqual(self.db.get_collection(target).config.params.vectors.size, 1536)
        self.assertEqual(target, bot_id)
        inputs = []
        for request in self.calls:
            self.assertEqual(str(request.url), embeddings.EMBEDDING_URL)
            self.assertEqual(request.headers["Authorization"], "Bearer test-openrouter-key")
            self.assertNotIn("key=", str(request.url))
            body = json.loads(request.content)
            self.assertEqual(body["model"], "openai/text-embedding-3-small")
            self.assertEqual(body["dimensions"], 1536)
            self.assertEqual(body["encoding_format"], "float")
            self.assertNotIn("taskType", body)
            inputs.extend(body["input"])
        self.assertEqual(len(inputs), 3)

    async def test_existing_openai_collection_is_reused_without_reindexing(self):
        bot_id = str(uuid.uuid4())
        self.db.create_collection(bot_id, vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
        self.db.upsert(bot_id, points=[PointStruct(id=1, vector=[1.0] + [0.0] * 1535,
                                                   payload={"text": "existing apple chunk"})])
        await self.schema.ensure_collection(bot_id)
        self.assertEqual(self.calls, [])
        result = await self.retrieval.retrieve_relevant_chunks(bot_id, "apple question", top_k=1)
        self.assertEqual(result[0]["text"], "existing apple chunk")
        self.assertEqual(self.db.count(bot_id).count, 1)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(ValueError, "do not need migration"):
            await self.migration.reindex_bot(bot_id, bot_id)
        self.assertEqual(len(self.calls), 1)

    async def test_migration_preserves_source_ids_payload_and_can_resume(self):
        bot_id = str(uuid.uuid4())
        source = bot_id + "__gemini_embedding_001_768_v1"
        self.db.create_collection(source, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
        ids = [str(uuid.uuid4()) for _ in range(3)]
        self.db.upsert(source, points=[
            PointStruct(id=point_id, vector=[1.0] + [0.0] * 767,
                        payload={"text": "apple chunk", "doc_id": "doc", "page":index})
            for index, point_id in enumerate(ids)
        ])
        self.assertEqual(await self.migration.reindex_bot(bot_id, source, page_size=1), 3)
        self.assertEqual(await self.migration.reindex_bot(bot_id, source, page_size=2), 3)
        target = embeddings.collection_for_bot(bot_id)
        self.assertEqual(self.db.count(source).count, 3)
        self.assertEqual(self.db.count(target).count, 3)
        old = {str(point.id):point for point in self.db.retrieve(source, ids, with_vectors=True)}
        new = {str(point.id):point for point in self.db.retrieve(target, ids, with_vectors=True)}
        for point_id in ids:
            self.assertEqual(old[point_id].payload, new[point_id].payload)
            self.assertEqual(len(old[point_id].vector), 768)
            self.assertEqual(len(new[point_id].vector), 1536)

    async def test_incompatible_target_is_rejected_without_deleting_it(self):
        target = embeddings.collection_for_bot("bot")
        self.db.create_collection(target, vectors_config=VectorParams(size=768, distance=Distance.COSINE))
        with self.assertRaisesRegex(ValueError, "vector schema"):
            await self.schema.ensure_collection("bot")
        self.assertEqual(self.db.get_collection(target).config.params.vectors.size, 768)

    async def test_missing_source_and_text_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            await self.migration.reindex_bot("missing", "missing-source")
        self.db.create_collection("invalid-source", vectors_config=VectorParams(size=768, distance=Distance.COSINE))
        self.db.upsert("invalid-source", points=[PointStruct(id=1, vector=[1.0] * 768, payload={})])
        with self.assertRaisesRegex(ValueError, "no text"):
            await self.migration.reindex_bot("invalid", "invalid-source")
        self.assertEqual(self.db.count("invalid-source").count, 1)

    async def test_empty_batch_and_bad_shape(self):
        self.assertEqual(await embeddings.embed_texts([]), [])
        self.assertEqual(self.calls, [])
        with self.assertRaisesRegex(ValueError, "between 1 and 100"):
            await embeddings.embed_texts(["text"], batch_size=0)
        self.response_body = {"data": [{"index": 0, "embedding": [0.0] * 768}]}
        with self.assertRaisesRegex(ValueError, "shape"):
            await embeddings.embed_texts(["text"])

    async def test_batching_order_and_normalization(self):
        result = await embeddings.embed_texts(["apple", "orange", "apple"], batch_size=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([len(json.loads(call.content)["input"]) for call in self.calls], [2, 1])
        self.assertEqual([vector[:2] for vector in result], [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])

    async def test_retry_limit_and_auth_errors(self):
        self.statuses = [429, 503, 200]
        with patch.object(embeddings.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(len(await embeddings.embed_texts(["apple"])), 1)
            self.assertEqual(sleep.await_count, 2)
        self.statuses = [429, 429, 429]
        with patch.object(embeddings.asyncio, "sleep", new_callable=AsyncMock):
            with self.assertRaises(httpx.HTTPStatusError):
                await embeddings.embed_texts(["apple"])
        for status in [401, 402, 403]:
            self.statuses = [status]
            before = len(self.calls)
            with self.assertRaises(httpx.HTTPStatusError):
                await embeddings.embed_texts(["apple"])
            self.assertEqual(len(self.calls) - before, 1)

    async def test_invalid_api_responses_and_inputs(self):
        for data in [
            {"data": []},
            {"data": [{"index": 0, "embedding": [0.0] * 1536}]},
            {"data": [{"index": 0, "embedding": ["bad"] * 1536}]},
        ]:
            self.response_body = data
            with self.assertRaises(ValueError):
                await embeddings.embed_texts(["apple"])
        with self.assertRaisesRegex(ValueError, "non-empty"):
            await embeddings.embed_texts([" "])
        with self.assertRaisesRegex(ValueError, "indices"):
            embeddings._normalized_vectors({"data": [{"index": 1, "embedding": [1.0] * 1536}]}, 1)


if __name__ == "__main__":
    unittest.main()
