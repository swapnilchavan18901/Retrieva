"""Ownership, quota, revocation, and deletion regressions."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from src.resource_limits import enforce_quota


class ResourceApiTests(unittest.TestCase):
    def setUp(self):
        self.db = NS(
            bot=NS(find_unique=AsyncMock(return_value=NS(id='bot', userId='owner')), count=AsyncMock(return_value=0),
                   create=AsyncMock(return_value=NS(id='bot', name='Test', createdAt=datetime.now(timezone.utc))), delete=AsyncMock()),
            apikey=NS(find_unique=AsyncMock(return_value=NS(id='key', botId='bot')), count=AsyncMock(return_value=0),
                      create=AsyncMock(return_value=NS(id='key')), update=AsyncMock(), delete=AsyncMock()),
            chatsession=NS(delete_many=AsyncMock()), chatrequest=NS(delete_many=AsyncMock()), query_raw=AsyncMock(),
        )
        @asynccontextmanager
        async def tx(**kwargs):
            yield self.db
        self.db.tx = tx
        self.modules = {
            'src.db': NS(prisma=self.db), 'src.deps.auth': NS(get_current_user=lambda: NS(id='owner')),
            'src.env': NS(env=NS(MAX_BOTS_PER_USER=10, MAX_API_KEYS_PER_BOT=10)),
        }
        with patch.dict(sys.modules, self.modules):
            root = Path(__file__).resolve().parents[1]
            bots = runpy.run_path(str(root / 'src/routes/bots.py'))
            keys = runpy.run_path(str(root / 'src/routes/api_key.py'))
        app = FastAPI()
        app.include_router(bots['router'])
        app.include_router(keys['router'])
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_bot_and_key_quota_reject_before_creation(self):
        self.db.bot.count.return_value = 10
        self.assertEqual(self.client.post('/bots', json={'name': 'Test'}).status_code, 409)
        self.db.bot.create.assert_not_awaited()
        self.db.apikey.count.return_value = 10
        self.assertEqual(self.client.post('/bots/bot/api-keys').status_code, 409)
        self.db.apikey.create.assert_not_awaited()

    def test_create_key_returns_its_id_and_counts_only_active_keys(self):
        response = self.client.post('/bots/bot/api-keys')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['id'], 'key')
        self.assertNotIn('keyHash', response.json())
        self.db.apikey.count.assert_awaited_once_with(where={'botId': 'bot', 'isActive': True})

    def test_key_revocation_preserves_history_foreign_keys(self):
        self.assertEqual(self.client.delete('/api-keys/key').status_code, 200)
        self.db.apikey.update.assert_awaited_once_with(where={'id': 'key'}, data={'isActive': False})
        self.db.apikey.delete.assert_not_awaited()

    def test_missing_and_other_owner_resources_keep_their_status_codes(self):
        self.db.bot.find_unique.return_value = None
        self.assertEqual(self.client.post('/bots/bot/api-keys').status_code, 404)
        self.assertEqual(self.client.get('/bots/bot/api-keys').status_code, 404)
        self.db.bot.find_unique.return_value = NS(userId='other')
        self.assertEqual(self.client.delete('/api-keys/key').status_code, 403)
        self.db.apikey.find_unique.return_value = None
        self.assertEqual(self.client.delete('/api-keys/key').status_code, 404)

    def test_bot_deletion_removes_history_before_parent(self):
        self.assertEqual(self.client.delete('/bots/bot').status_code, 200)
        self.db.chatrequest.delete_many.assert_awaited_once_with(where={'botId': 'bot'})
        self.db.chatsession.delete_many.assert_awaited_once_with(where={'botId': 'bot'})
        self.db.bot.delete.assert_awaited_once_with(where={'id': 'bot'})

    def test_blank_names_and_explicit_null_fields_are_rejected(self):
        for body in ({'name': '  '}, {'name': None}, {'isActive': None}, {'systemPrompt': 'x' * 16_001}):
            self.assertEqual(self.client.put('/bots/bot', json=body).status_code, 422)


@unittest.skipUnless(os.environ.get('RETRIEVA_LIMIT_TEST_DATABASE_URL'), 'disposable local DB not configured')
class PostgresQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from generated.prisma import Prisma
        url = os.environ['RETRIEVA_LIMIT_TEST_DATABASE_URL']
        parsed = urlparse(url)
        if parsed.hostname not in {'localhost', '127.0.0.1'} or parsed.port != 55433:
            raise RuntimeError('Tests require disposable PostgreSQL on localhost:55433')
        self.clients = [Prisma(datasource={'url': url}), Prisma(datasource={'url': url})]
        for client in self.clients:
            await client.connect()
        self.user = await self.clients[0].user.create(data={'email': f'{uuid4()}@test.invalid', 'passwordHash': 'test'})

    async def asyncTearDown(self):
        try:
            await self.clients[0].user.delete(where={'id': self.user.id})
        finally:
            for client in self.clients:
                await client.disconnect()

    async def test_creation_race_cannot_exceed_quota(self):
        async def create(client):
            async with client.tx() as tx:
                await enforce_quota(tx, 'Assistant', self.user.id, 'bot', {'userId': self.user.id}, 1)
                return await tx.bot.create(data={'name': 'Test', 'userId': self.user.id})
        results = await asyncio.gather(*(create(client) for client in self.clients), return_exceptions=True)
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        denied = next(item for item in results if isinstance(item, Exception))
        self.assertIsInstance(denied, HTTPException)
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(await self.clients[0].bot.count(where={'userId': self.user.id}), 1)

    async def test_revoke_used_key_then_delete_bot_with_real_history(self):
        db = self.clients[0]
        bot = await db.bot.create(data={'name': 'History test', 'userId': self.user.id})
        key = await db.apikey.create(data={'botId': bot.id, 'keyHash': str(uuid4())})
        session = await db.chatsession.create(data={'botId': bot.id, 'apiKeyId': key.id})
        await db.chatmessage.create(data={'sessionId': session.id, 'role': 'user', 'content': 'Test'})
        await db.chatrequest.create(data={'botId': bot.id, 'apiKeyId': key.id, 'question': 'Test', 'response': 'Test'})
        with patch.dict(sys.modules, {
            'src.db': NS(prisma=db), 'src.deps.auth': NS(get_current_user=lambda: self.user),
            'src.env': NS(env=NS(MAX_BOTS_PER_USER=10, MAX_API_KEYS_PER_BOT=1)),
        }):
            root = Path(__file__).resolve().parents[1]
            keys = runpy.run_path(str(root / 'src/routes/api_key.py'))
            bots = runpy.run_path(str(root / 'src/routes/bots.py'))
        await keys['revoke_api_key'](key.id, self.user)
        self.assertFalse((await db.apikey.find_unique(where={'id': key.id})).isActive)
        self.assertIsNotNone(await db.chatsession.find_unique(where={'id': session.id}))
        # A revoked key no longer consumes the one-active-key allowance.
        created = await keys['create_api_key'](bot.id, self.user)
        self.assertNotEqual(created['id'], key.id)
        await bots['delete_bot'](bot.id, self.user)
        self.assertIsNone(await db.bot.find_unique(where={'id': bot.id}))
        self.assertIsNone(await db.chatsession.find_unique(where={'id': session.id}))
