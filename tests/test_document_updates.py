"""Document API and worker orchestration checks with isolated fake services."""
import importlib
import os
import sys
import tempfile
import types
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from shared import benchmark

from fastapi import FastAPI
from fastapi.testclient import TestClient


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


class DocumentAPITests(unittest.TestCase):
    def setUp(self):
        benchmark_patch = patch.object(benchmark, "emit_event")
        benchmark_patch.start()
        self.addCleanup(benchmark_patch.stop)
        self.db = ns(bot=ns(find_unique=AsyncMock(return_value=ns(id='bot', userId='user'))),
            document=ns(find_unique=AsyncMock(return_value=ns(id='doc', botId='bot', bot=ns(userId='user'), status='ready')),
                        create=AsyncMock(), find_many=AsyncMock(return_value=[])),
            documentupload=ns(create=AsyncMock(), find_unique=AsyncMock(), find_first=AsyncMock(return_value=None),
                              update_many=AsyncMock(return_value=1)))
        @asynccontextmanager
        async def tx():
            yield self.db
        self.db.tx = tx
        self.publish = AsyncMock()
        modules = patch.dict(sys.modules, {
            'src.db': ns(prisma=self.db),
            'src.deps.auth': ns(get_current_user=lambda: ns(id='user')),
            'src.utils.s3': ns(generate_presigned_upload_url=lambda key: 'https://example.invalid/upload'),
            'src.producer.producer': ns(publish_document_job=self.publish),
        })
        modules.start()
        self.addCleanup(modules.stop)
        routes = importlib.reload(importlib.import_module('src.routes.documenation'))
        app = FastAPI()
        app.include_router(routes.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.body = {'fileName': 'updated.pdf', 'fileType': 'application/pdf', 'fileSize': 100, 'documentId': 'doc'}

    def test_explicit_update_keeps_identity_and_uses_new_s3_object(self):
        response = self.client.post('/bots/bot/documents/upload-url', json=self.body)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['documentId'], 'doc')
        self.assertIn(body['uploadId'], body['s3Key'])
        self.db.document.create.assert_not_awaited()
        created = self.db.documentupload.create.await_args.kwargs['data']
        self.assertEqual(created['documentId'], 'doc')
        self.assertEqual(created['fileName'], 'updated.pdf')

    def test_new_pdf_gets_new_identity(self):
        response = self.client.post('/bots/bot/documents/upload-url', json={**self.body, 'documentId': None})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()['documentId'], 'doc')
        self.db.document.create.assert_awaited_once()

    def test_other_users_and_documents_cannot_be_updated(self):
        self.db.bot.find_unique.return_value.userId = 'other'
        self.assertEqual(self.client.post('/bots/bot/documents/upload-url', json=self.body).status_code, 404)
        self.db.documentupload.create.assert_not_awaited()
        self.db.bot.find_unique.return_value.userId = 'user'
        self.db.document.find_unique.return_value.botId = 'different-bot'
        self.assertEqual(self.client.post('/bots/bot/documents/upload-url', json=self.body).status_code, 404)

    def test_complete_binds_upload_to_document_and_is_idempotent(self):
        upload = ns(id='upload', documentId='other', status='pending')
        self.db.documentupload.find_unique.return_value = upload
        self.assertEqual(self.client.post('/documents/doc/complete', json={'uploadId': 'upload'}).status_code, 404)
        self.publish.assert_not_awaited()
        upload.documentId = 'doc'
        self.assertEqual(self.client.post('/documents/doc/complete', json={'uploadId': 'upload'}).status_code, 200)
        self.publish.assert_awaited_once_with('doc', 'bot', 'user', 'upload')
        upload.status = 'ready'
        self.assertEqual(self.client.post('/documents/doc/complete', json={'uploadId': 'upload'}).status_code, 200)
        self.assertEqual(self.publish.await_count, 1)

    def test_queue_failure_can_be_retried(self):
        self.db.documentupload.find_unique.return_value = ns(id='upload', documentId='doc', status='pending')
        self.publish.side_effect = RuntimeError('broker unavailable')
        response = self.client.post('/documents/doc/complete', json={'uploadId': 'upload'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.db.documentupload.update_many.await_args.kwargs['data']['status'], 'pending')

    def test_rejects_empty_large_and_non_pdf_files(self):
        for body in ({**self.body, 'fileSize': 0}, {**self.body, 'fileSize': 51 * 1024 * 1024},
                     {**self.body, 'fileType': 'text/plain'}, {**self.body, 'fileName': 'file.exe'}):
            self.assertGreaterEqual(self.client.post('/bots/bot/documents/upload-url', json=body).status_code, 400)
        self.db.documentupload.create.assert_not_awaited()


class DocumentWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        benchmark_patch = patch.object(benchmark, "emit_event")
        benchmark_patch.start()
        self.addCleanup(benchmark_patch.stop)
        self.events = []
        now = datetime.now(timezone.utc)
        self.doc = ns(id='doc', botId='bot', bot=ns(userId='user'), activeRevision='v1',
                      activeRevisionCreatedAt=now - timedelta(hours=1))
        self.upload = ns(id='v2', documentId='doc', status='queued', createdAt=now,
                         storageUrl='new-object.pdf', fileName='new.pdf', fileType='application/pdf', fileSize=100)
        async def update_document(**kwargs):
            self.events.append('activate')
            for key, value in kwargs['data'].items():
                setattr(self.doc, key, value)
        self.db = ns(document=ns(find_unique=AsyncMock(side_effect=lambda **kwargs: self.doc),
                                update=AsyncMock(side_effect=update_document), update_many=AsyncMock()),
                     documentupload=ns(find_unique=AsyncMock(side_effect=lambda **kwargs: self.upload),
                                       update=AsyncMock(), update_many=AsyncMock()), query_raw=AsyncMock())
        @asynccontextmanager
        async def tx(**kwargs):
            self.events.append('begin')
            yield self.db
            self.events.append('commit')
        self.db.tx = tx
        temporary = tempfile.NamedTemporaryFile(delete=False)
        temporary.close()
        self.temp_path = temporary.name
        self.addCleanup(lambda: os.path.exists(self.temp_path) and os.unlink(self.temp_path))
        self.load = AsyncMock(return_value=([], self.temp_path))
        self.stage = AsyncMock(side_effect=lambda *args, **kwargs: self.events.append('stage'))
        self.cleanup = AsyncMock(side_effect=lambda *args: self.events.append('cleanup'))
        modules = patch.dict(sys.modules, {
            'consumer.consumer_db': ns(prisma=self.db),
            'consumer.consumer_env': ns(env=ns(RABBIT_MQ_URL='amqp://localhost')),
            'consumer.aws.file_loader': ns(load_pdf_from_s3=self.load),
            'consumer.vector.insert': ns(embed_and_store=self.stage, cleanup_revision=self.cleanup),
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.worker = importlib.reload(importlib.import_module('consumer.consumer'))
        self.job = {'document_id': 'doc', 'bot_id': 'bot', 'user_id': 'user', 'upload_id': 'v2'}

    async def test_activation_commits_before_cleanup_and_temporary_file_is_removed(self):
        await self.worker.process_document(self.job)
        self.assertEqual(self.events, ['begin', 'stage', 'activate', 'commit', 'begin', 'cleanup', 'commit'])
        self.assertEqual(self.doc.activeRevision, 'v2')
        self.assertFalse(os.path.exists(self.temp_path))
        self.assertEqual(self.db.query_raw.await_count, 2)

    async def test_failure_never_activates_or_cleans_old_revision(self):
        self.stage.side_effect = RuntimeError('embeddings failed')
        with self.assertRaises(RuntimeError):
            await self.worker.process_document(self.job)
        self.assertEqual(self.doc.activeRevision, 'v1')
        self.db.document.update.assert_not_awaited()
        self.cleanup.assert_not_awaited()
        self.assertFalse(os.path.exists(self.temp_path))
        self.db.documentupload.update_many.assert_awaited_once()

    async def test_duplicate_delivery_does_not_reembed(self):
        self.doc.activeRevision = 'v2'
        await self.worker.process_document(self.job)
        self.stage.assert_not_awaited()
        self.load.assert_not_awaited()
        self.cleanup.assert_awaited_once()

    async def test_older_job_cannot_overwrite_newer_revision(self):
        self.doc.activeRevisionCreatedAt = self.upload.createdAt + timedelta(minutes=1)
        await self.worker.process_document(self.job)
        self.stage.assert_not_awaited()
        self.db.document.update.assert_not_awaited()
        self.assertEqual(self.db.documentupload.update.await_args.kwargs['data']['status'], 'superseded')

    async def test_job_ownership_is_verified_before_any_mutation(self):
        with self.assertRaises(ValueError):
            await self.worker.process_document({**self.job, 'user_id': 'other'})
        self.stage.assert_not_awaited()
        self.db.document.update_many.assert_not_awaited()
        self.db.documentupload.update_many.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
