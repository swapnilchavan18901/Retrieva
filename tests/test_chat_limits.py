"""Run DB races only against an explicitly supplied disposable local PostgreSQL."""
import asyncio
import os
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import uuid4

from src.sockets.limits import ChatLimits, LimitDenied, DAILY_OUTPUT_TOKEN_LIMIT, ip_hash, validate_message, windows, INDIA


class InputLimitTests(unittest.TestCase):
    def test_invalid_and_oversized_payloads(self):
        for value in (None, [], {}, {'content': 12}, {'content': '   '}, {'content': 'x' * 4001}):
            with self.assertRaises(LimitDenied):
                validate_message(value)
        validate_message({'content': 'x' * 4000})

    def test_forwarded_header_cannot_spoof_client(self):
        self.assertEqual(ip_hash({'REMOTE_ADDR': '1.2.3.4'}), ip_hash({'REMOTE_ADDR': '1.2.3.4', 'HTTP_X_FORWARDED_FOR': '8.8.8.8'}))
        self.assertNotEqual(ip_hash({'REMOTE_ADDR': '1.2.3.4'}), ip_hash({'REMOTE_ADDR': '1.2.3.5'}))

    def test_fixed_window_reset_has_exact_retry(self):
        now = datetime(2026, 10, 30, 12, 0, 59, tzinfo=timezone.utc)
        item = next(windows(now, [('messages', 'visitor', 10, 60)]))
        self.assertEqual(item[3], 1)
        self.assertEqual(item[2], now + timedelta(seconds=1))
        self.assertEqual(DAILY_OUTPUT_TOKEN_LIMIT, 50_000)


@unittest.skipUnless(os.environ.get('RETRIEVA_LIMIT_TEST_DATABASE_URL'), 'disposable local DB not configured')
class PostgresLimitTests(unittest.IsolatedAsyncioTestCase):
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
        await self.db.execute_raw('DELETE FROM "ChatLimitLease"')
        await self.db.execute_raw('DELETE FROM "ChatLimitCounter"')
        user = await self.db.user.create(data={'email': f'{uuid4()}@test.invalid', 'passwordHash': 'test'})
        self.user = user.id
        self.bot = await self.db.bot.create(data={'userId': user.id, 'name': 'Test'})
        self.key = await self.db.apikey.create(data={'botId': self.bot.id, 'keyHash': str(uuid4())})
        self.guard = ChatLimits(self.db)
        self.second = ChatLimits(self.other)
        self.session = await self.new_session()

    async def new_session(self, key=None, address=None):
        key = key or self.key.id
        chat = await self.db.chatsession.create(data={'botId': self.bot.id, 'apiKeyId': key})
        return {'botId': self.bot.id, 'apiKeyId': key, 'sessionId': chat.id, 'ipHash': address or str(uuid4())}

    async def asyncTearDown(self):
        try:
            await self.db.chatsession.delete_many(where={'botId': self.bot.id})
            await self.db.user.delete(where={'id': self.user})
        finally:
            await self.db.disconnect()
            await self.other.disconnect()

    async def daily_counter(self):
        rows = await self.db.query_raw('SELECT "value","expiresAt","key" FROM "ChatLimitCounter" WHERE "key" LIKE $1', f'output:{self.bot.id}:%')
        return rows[0]

    async def test_shared_session_race_admits_one_across_clients(self):
        results = await asyncio.gather(self.guard.acquire(self.session), self.second.acquire(self.session), return_exceptions=True)
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 1)
        error = next(item for item in results if isinstance(item, LimitDenied))
        self.assertEqual(error.code, 'reply_in_progress')

    async def test_daily_cap_across_keys_and_partial_final_allowance(self):
        initial = await self.guard.acquire(self.session)
        row = await self.daily_counter()
        await self.guard.finish(initial, 0)
        await self.db.execute_raw('UPDATE "ChatLimitCounter" SET "value"=49950 WHERE "key"=$1', row['key'])
        another_key = await self.db.apikey.create(data={'botId': self.bot.id, 'keyHash': str(uuid4())})
        other_session = await self.new_session(another_key.id)
        results = await asyncio.gather(self.guard.acquire(self.session), self.second.acquire(other_session), return_exceptions=True)
        accepted = next(item for item in results if not isinstance(item, Exception))
        self.assertEqual(accepted.max_tokens, 50)
        self.assertEqual(next(item for item in results if isinstance(item, LimitDenied)).code, 'daily_token_limit')
        self.assertEqual((await self.daily_counter())['value'], 50_000)
        await self.guard.finish(accepted, 25)
        self.assertEqual((await self.daily_counter())['value'], 49_975)
        await self.guard.finish(accepted, 0)  # Duplicate completion must not refund twice.
        self.assertEqual((await self.daily_counter())['value'], 49_975)

    async def test_unknown_usage_retains_reservation_and_releases_concurrency(self):
        admission = await self.guard.acquire(self.session)
        await self.guard.finish(admission)
        self.assertEqual((await self.daily_counter())['value'], 768)
        again = await self.guard.acquire(self.session)
        await self.guard.finish(again, 0)
        self.assertEqual((await self.daily_counter())['value'], 768)

    async def test_revoked_key_rejected(self):
        await self.db.apikey.update(where={'id': self.key.id}, data={'isActive': False})
        with self.assertRaises(LimitDenied) as caught:
            await self.guard.acquire(self.session)
        self.assertEqual(caught.exception.code, 'unauthorized')

    async def test_ip_message_and_session_limits_survive_new_guard(self):
        for _ in range(10):
            admission = await self.guard.acquire(self.session)
            await self.guard.finish(admission, 0)
        with self.assertRaises(LimitDenied) as caught:
            await self.second.acquire(self.session)
        self.assertEqual(caught.exception.code, 'rate_limited')
        self.assertGreater(caught.exception.retry_after, 0)

    async def test_bot_concurrency_cap(self):
        sessions = [await self.new_session() for _ in range(6)]
        results = await asyncio.gather(*(self.guard.acquire(item) for item in sessions), return_exceptions=True)
        self.assertEqual(sum(not isinstance(item, Exception) for item in results), 5)
        self.assertEqual(next(item for item in results if isinstance(item, LimitDenied)).code, 'busy')

    async def test_ip_rate_limit_is_shared_across_sessions(self):
        for _ in range(2):
            session = await self.new_session(address='shared-ip')
            for _ in range(10):
                admission = await self.guard.acquire(session)
                await self.guard.finish(admission, 0)
        with self.assertRaises(LimitDenied) as caught:
            await self.second.acquire(await self.new_session(address='shared-ip'))
        self.assertEqual(caught.exception.code, 'rate_limited')

    async def test_key_rate_limit_is_shared_across_visitors(self):
        for _ in range(6):
            session = await self.new_session()
            for _ in range(10):
                admission = await self.guard.acquire(session)
                await self.guard.finish(admission, 0)
        with self.assertRaises(LimitDenied) as caught:
            await self.second.acquire(await self.new_session())
        self.assertEqual(caught.exception.code, 'rate_limited')

    async def test_ip_and_global_generation_concurrency(self):
        first = await self.new_session(address='shared-ip')
        second = await self.new_session(address='shared-ip')
        await self.guard.acquire(first)
        await self.second.acquire(second)
        with self.assertRaises(LimitDenied) as caught:
            await self.guard.acquire(await self.new_session(address='shared-ip'))
        self.assertEqual(caught.exception.code, 'busy')
        await self.db.execute_raw('DELETE FROM "ChatLimitLease"')
        await self.db.execute_raw('''INSERT INTO "ChatLimitLease" ("id","kind","botId","apiKeyId","sessionId","ipHash","expiresAt")
            SELECT 'fake-'||i,'generation','other-bot','key','other-'||i,'ip-'||i,CURRENT_TIMESTAMP + INTERVAL '2 minutes'
            FROM generate_series(1,20) i''')
        with self.assertRaises(LimitDenied) as caught:
            await self.guard.acquire(self.session)
        self.assertEqual(caught.exception.code, 'busy')

    async def test_crashed_generation_keeps_token_charge_but_releases_slot(self):
        admission = await self.guard.acquire(self.session)
        await self.db.execute_raw('''UPDATE "ChatLimitLease" SET "expiresAt"=CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE "id"=$1''', admission.id)
        again = await self.second.acquire(self.session)
        self.assertEqual((await self.daily_counter())['value'], 1536)
        await self.guard.finish(admission, 0)  # Expired lease cannot refund twice.
        await self.guard.finish(again, 0)
        self.assertEqual((await self.daily_counter())['value'], 768)

    async def test_connections_renew_disconnect_and_crash_expiry(self):
        for index in range(5):
            await self.guard.connect(f'connection-{index}', self.bot.id, self.key.id, 'same-ip')
        with self.assertRaises(LimitDenied):
            await self.second.connect('extra', self.bot.id, self.key.id, 'same-ip')
        self.assertTrue(await self.guard.renew('connection-0'))
        await self.guard.disconnect('connection-0')
        await self.second.connect('extra', self.bot.id, self.key.id, 'same-ip')
        await self.db.execute_raw('UPDATE "ChatLimitLease" SET "expiresAt"=CURRENT_TIMESTAMP - INTERVAL \'1 second\' WHERE "kind"=\'connection\'')
        self.assertFalse(await self.guard.renew('extra'))
        await self.second.connect('after-crash', self.bot.id, self.key.id, 'same-ip')

    async def test_connection_attempt_throttle(self):
        for _ in range(20):
            await self.guard.connection_attempt('ip')
        with self.assertRaises(LimitDenied):
            await self.second.connection_attempt('ip')

    async def test_reset_uses_india_day_and_old_settlement_keeps_old_day(self):
        admission = await self.guard.acquire(self.session)
        row = await self.daily_counter()
        self.assertTrue(row['key'].endswith(str(datetime.now(timezone.utc).astimezone(INDIA).date())))
        yesterday = f'output:{self.bot.id}:yesterday'
        await self.db.execute_raw('UPDATE "ChatLimitCounter" SET "key"=$2 WHERE "key"=$1', row['key'], yesterday)
        await self.db.execute_raw('UPDATE "ChatLimitLease" SET "dayKey"=$2 WHERE "id"=$1', admission.id, yesterday)
        other = await self.guard.acquire(await self.new_session())
        await self.guard.finish(admission, 10)
        rows = await self.db.query_raw('SELECT "value" FROM "ChatLimitCounter" WHERE "key"=$1', yesterday)
        self.assertEqual(rows[0]['value'], 10)
        current = await self.db.query_raw('SELECT "value" FROM "ChatLimitCounter" WHERE "key"=$1', row['key'])
        self.assertEqual(current[0]['value'], other.max_tokens)
