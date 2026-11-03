"""Confirmed outbox dispatch and finite worker retries."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import uuid4

from src.producer.outbox import dispatch_one, enqueue_document_job


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = NS(query_raw=AsyncMock(return_value=[{'id': 'upload', 'payload': {
            'document_id': 'doc', 'bot_id': 'bot', 'user_id': 'owner', 'upload_id': 'upload'}, 'attempts': 0}]), execute_raw=AsyncMock())
        @asynccontextmanager
        async def tx(**kwargs):
            yield self.db
        self.db.tx = tx
        self.publish = AsyncMock()

    async def test_success_deletes_intent_only_after_confirmation(self):
        await dispatch_one(self.db, self.publish)
        self.publish.assert_awaited_once_with('doc', 'bot', 'owner', 'upload')
        self.assertIn('DELETE', self.db.execute_raw.await_args.args[0])

    async def test_broker_failure_keeps_intent_and_schedules_backoff(self):
        self.publish.side_effect = RuntimeError('broker unavailable')
        await dispatch_one(self.db, self.publish)
        args = self.db.execute_raw.await_args.args
        self.assertIn('UPDATE', args[0])
        self.assertEqual(args[2], 2)

    async def test_cancelled_delivery_does_not_delete_intent(self):
        self.publish.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await dispatch_one(self.db, self.publish)
        self.db.execute_raw.assert_not_awaited()


class WorkerRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.dict(sys.modules, {
            'consumer.consumer_env': NS(env=NS(RABBIT_MQ_URL='amqp://test')),
            'consumer.consumer_db': NS(prisma=NS()),
            'consumer.aws.file_loader': NS(load_pdf_from_s3=AsyncMock()),
            'consumer.vector.insert': NS(embed_and_store=AsyncMock(), cleanup_revision=AsyncMock()),
        }):
            self.module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'consumer/consumer.py'))
        self.handler = self.module['on_message']
        self.process = AsyncMock()
        self.publish = AsyncMock()
        self.handler.__globals__.update(process_document=self.process, publish_retry=self.publish)
        sleep = patch('asyncio.sleep', new=AsyncMock())
        sleep.start()
        self.addCleanup(sleep.stop)
        self.message = NS(body=b'{"document_id":"doc"}', headers={}, message_id='job', ack=AsyncMock(), reject=AsyncMock())

    async def test_success_is_acknowledged_without_republication(self):
        await self.handler(self.message)
        self.message.ack.assert_awaited_once()
        self.publish.assert_not_awaited()

    async def test_transient_failure_is_retried_with_incremented_attempt(self):
        self.process.side_effect = RuntimeError('temporary')
        await self.handler(self.message)
        self.assertEqual(self.publish.await_args.args[1:], ('document_process_queue', 1))
        self.message.ack.assert_awaited_once()

    async def test_exhausted_failure_goes_to_dead_letter_queue(self):
        self.message.headers = {'x-retrieva-retry': 3}
        self.process.side_effect = RuntimeError('temporary')
        await self.handler(self.message)
        self.assertEqual(self.publish.await_args.args[1], 'document_process_dead_letter_queue')
        self.message.ack.assert_awaited_once()

    async def test_malformed_job_goes_directly_to_dead_letter_queue(self):
        self.message.body = b'[]'
        await self.handler(self.message)
        self.process.assert_not_awaited()
        self.assertEqual(self.publish.await_args.args[1], 'document_process_dead_letter_queue')

    async def test_recovery_publish_failure_preserves_original(self):
        self.process.side_effect = RuntimeError('temporary')
        self.publish.side_effect = RuntimeError('broker down')
        await self.handler(self.message)
        self.message.ack.assert_not_awaited()
        self.message.reject.assert_awaited_once_with(requeue=True)


@unittest.skipUnless(os.environ.get('RETRIEVA_LIMIT_TEST_DATABASE_URL'), 'disposable local DB not configured')
class PostgresOutboxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from generated.prisma import Prisma
        url = os.environ['RETRIEVA_LIMIT_TEST_DATABASE_URL']
        parsed = urlparse(url)
        if parsed.hostname not in {'localhost', '127.0.0.1'} or parsed.port != 55433:
            raise RuntimeError('Tests require disposable PostgreSQL on localhost:55433')
        self.clients = [Prisma(datasource={'url': url}), Prisma(datasource={'url': url})]
        for client in self.clients:
            await client.connect()
        self.identity = str(uuid4())
        self.publish = AsyncMock()
        await enqueue_document_job(self.clients[0], 'doc', 'bot', 'owner', self.identity)

    async def asyncTearDown(self):
        try:
            await self.clients[0].execute_raw('DELETE FROM "DocumentJobOutbox" WHERE "id"=$1', self.identity)
        finally:
            for client in self.clients:
                await client.disconnect()

    async def test_two_dispatchers_claim_one_job_once(self):
        await asyncio.gather(*(dispatch_one(client, self.publish) for client in self.clients))
        self.publish.assert_awaited_once_with('doc', 'bot', 'owner', self.identity)
        self.assertEqual(await self.clients[0].query_raw('SELECT "id" FROM "DocumentJobOutbox" WHERE "id"=$1', self.identity), [])

    async def test_failed_delivery_survives_and_can_be_retried(self):
        self.publish.side_effect = RuntimeError('offline')
        await dispatch_one(self.clients[0], self.publish)
        rows = await self.clients[0].query_raw('SELECT "attempts" FROM "DocumentJobOutbox" WHERE "id"=$1', self.identity)
        self.assertEqual(rows[0]['attempts'], 1)
        await self.clients[0].execute_raw('UPDATE "DocumentJobOutbox" SET "nextAttemptAt"=CURRENT_TIMESTAMP - INTERVAL \'1 second\' WHERE "id"=$1', self.identity)
        self.publish.side_effect = None
        await dispatch_one(self.clients[1], self.publish)
        self.assertEqual(await self.clients[0].query_raw('SELECT "id" FROM "DocumentJobOutbox" WHERE "id"=$1', self.identity), [])
