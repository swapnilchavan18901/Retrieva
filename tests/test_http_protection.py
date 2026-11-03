"""Exercise HTTP protections without credentials or external services."""

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from src.http_limits import HttpRateLimiter, RatePolicy
from src.middleware import ApiProtectionMiddleware


class HttpProtectionTests(unittest.TestCase):
    def setUp(self):
        self.limiter = SimpleNamespace(check=AsyncMock(return_value=0))
        app = FastAPI()
        app.add_middleware(ApiProtectionMiddleware, limiter=self.limiter, max_body_bytes=32)
        app.add_middleware(CORSMiddleware, allow_origins=["https://app.example.com"], allow_methods=["*"])
        self.calls = 0

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def endpoint(request: Request):
            self.calls += 1
            return {"size": len(await request.body())}

        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_login_has_ip_and_auth_limits_and_does_not_trust_forwarded_headers(self):
        response = self.client.post('/auth/login', json={}, headers={"X-Forwarded-For": "spoofed"})
        self.assertEqual(response.status_code, 200)
        calls = self.limiter.check.await_args_list
        self.assertEqual([call.args[1].name for call in calls], ["all", "login"])
        self.assertEqual(calls[0].args[0], "testclient")

    def test_429_has_retry_after_cors_and_does_not_run_handler(self):
        self.limiter.check.return_value = 12
        response = self.client.post('/auth/login', json={}, headers={"Origin": "https://app.example.com"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['retry-after'], '12')
        self.assertEqual(response.headers['access-control-allow-origin'], 'https://app.example.com')
        self.assertEqual(self.calls, 0)

    def test_login_account_throttle_covers_multiple_source_ips(self):
        self.limiter.check.side_effect = [0, 0, 45]
        # Keep this body within the deliberately small test body ceiling.
        response = self.client.post('/auth/login', json={'email': 'A@B.COM'})
        self.assertEqual(response.status_code, 429)
        call = self.limiter.check.await_args_list[-1]
        self.assertEqual(call.args[0], 'a@b.com')
        self.assertEqual(call.args[1].name, 'login_account')
        self.assertEqual(response.headers['retry-after'], '45')
        self.assertEqual(self.calls, 0)

    def test_limiter_outage_fails_closed_without_disclosing_error(self):
        self.limiter.check.side_effect = RuntimeError("PRIVATE_DATABASE_PASSWORD")
        response = self.client.get('/bots')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('PRIVATE_DATABASE_PASSWORD', response.text)
        self.assertEqual(self.calls, 0)

    def test_health_preflight_and_socket_transport_do_not_require_rest_limiter(self):
        self.limiter.check.side_effect = RuntimeError('offline')
        for path in ('/', '/health', '/socket.io/'):
            self.assertEqual(self.client.get(path).status_code, 200)
        response = self.client.options('/bots', headers={'Origin': 'https://app.example.com', 'Access-Control-Request-Method': 'POST'})
        self.assertEqual(response.status_code, 200)
        self.limiter.check.assert_not_awaited()

    def test_content_length_and_chunked_oversized_bodies_are_rejected_before_handler(self):
        self.assertEqual(self.client.post('/bots', content=b'x' * 33).status_code, 413)
        self.assertEqual(self.client.post('/bots', content=iter([b'x' * 16, b'x' * 17])).status_code, 413)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.client.post('/bots', content=b'x' * 32).json()['size'], 32)

    def test_invalid_content_length(self):
        for length in ('bad', '-1'):
            self.assertEqual(self.client.post('/bots', content=b'', headers={'Content-Length': length}).status_code, 400)
        self.assertEqual(self.calls, 0)

    def test_request_ids_are_generated_and_private_responses_are_not_cached(self):
        first = self.client.get('/bots', headers={'X-Request-ID': 'untrusted'})
        second = self.client.get('/bots')
        self.assertNotEqual(first.headers['x-request-id'], 'untrusted')
        self.assertNotEqual(first.headers['x-request-id'], second.headers['x-request-id'])
        self.assertEqual(first.headers['cache-control'], 'no-store')
        self.assertEqual(first.headers['x-content-type-options'], 'nosniff')


@unittest.skipUnless(os.environ.get('RETRIEVA_LIMIT_TEST_DATABASE_URL'), 'disposable local DB not configured')
class PostgresHttpLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from generated.prisma import Prisma
        url = os.environ['RETRIEVA_LIMIT_TEST_DATABASE_URL']
        parsed = urlparse(url)
        if parsed.hostname not in {'localhost', '127.0.0.1'} or parsed.port != 55433:
            raise RuntimeError('Tests require disposable PostgreSQL on localhost:55433')
        self.db = Prisma(datasource={'url': url})
        self.other = Prisma(datasource={'url': url})
        await self.db.connect()
        await self.other.connect()
        self.guards = [HttpRateLimiter(self.db), HttpRateLimiter(self.other)]
        self.identity = str(uuid4())
        self.policy = RatePolicy('test', 5, 60)

    async def asyncTearDown(self):
        try:
            await self.db.execute_raw('DELETE FROM "ChatLimitCounter" WHERE "key" LIKE \'http:test:%\'')
        finally:
            await self.db.disconnect()
            await self.other.disconnect()

    async def test_concurrent_processes_admit_exactly_capacity(self):
        results = await asyncio.gather(*(self.guards[i % 2].check(self.identity, self.policy) for i in range(20)))
        self.assertEqual(results.count(0), 5)
        self.assertTrue(all(1 <= value <= 60 for value in results if value))
        self.assertEqual(await self.guards[0].check('another-ip', self.policy), 0)

    async def test_expired_window_reuses_counter_and_starts_at_one(self):
        await self.guards[0].check(self.identity, self.policy)
        await self.db.execute_raw('UPDATE "ChatLimitCounter" SET "expiresAt"=CURRENT_TIMESTAMP - INTERVAL \'1 second\' WHERE "key" LIKE \'http:test:%\'')
        self.assertEqual(await self.guards[1].check(self.identity, self.policy), 0)
        rows = await self.db.query_raw('SELECT "value" FROM "ChatLimitCounter" WHERE "key" LIKE \'http:test:%\'')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['value'], 1)
