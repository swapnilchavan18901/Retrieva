"""Authentication edge cases with no production database or credentials."""

from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from generated.prisma.errors import UniqueViolationError


class AuthHardeningTests(unittest.TestCase):
    def setUp(self):
        self.users = NS(find_unique=AsyncMock(return_value=None), create=AsyncMock())
        self.hash = Mock(return_value='hashed-password')
        self.verify = Mock(return_value=False)
        with patch.dict(sys.modules, {
            'src.db': NS(prisma=NS(user=self.users)),
            'src.env': NS(env=NS(DEMO_ENABLED=False)),
            'src.utils.password': NS(hash_password=self.hash, verify_password=self.verify,
                                      DUMMY_PASSWORD_HASH='dummy-hash', MAX_PASSWORD_BYTES=72),
            'src.utils.jwt': NS(create_access_token=Mock(return_value='session-token')),
        }):
            module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'src/routes/auth.py'))
        app = FastAPI()
        app.include_router(module['router'])
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.body = {'email': 'owner@example.com', 'password': 'test-password', 'name': 'Owner'}

    def test_registration_returns_only_public_fields(self):
        self.users.create.return_value = NS(id='user', email='owner@example.com', name='Owner', passwordHash='PRIVATE_HASH', role='admin')
        response = self.client.post('/auth/register', json=self.body)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()['user'], {'id': 'user', 'email': 'owner@example.com', 'name': 'Owner'})
        self.assertNotIn('PRIVATE_HASH', response.text)
        self.assertEqual(self.users.create.await_args.kwargs['data']['passwordHash'], 'hashed-password')

    def test_duplicate_and_concurrent_duplicate_registrations_return_conflict(self):
        self.users.find_unique.return_value = NS(id='user')
        self.assertEqual(self.client.post('/auth/register', json=self.body).status_code, 409)
        self.hash.assert_not_called()
        self.users.find_unique.return_value = None
        self.users.create.side_effect = UniqueViolationError({})
        self.assertEqual(self.client.post('/auth/register', json=self.body).status_code, 409)

    def test_bcrypt_byte_limit_and_invalid_inputs_are_rejected_before_db_work(self):
        for password in ('short', 'x' * 73, '😀' * 19):
            self.assertEqual(self.client.post('/auth/register', json={**self.body, 'password': password}).status_code, 422)
        self.users.find_unique.assert_not_awaited()
        self.hash.assert_not_called()

    def test_unknown_user_still_performs_password_check(self):
        response = self.client.post('/auth/login', json=self.body)
        self.assertEqual(response.status_code, 401)
        self.verify.assert_called_once_with('test-password', 'dummy-hash')
        self.assertEqual(response.headers['www-authenticate'], 'Bearer')

    def test_valid_and_invalid_existing_password(self):
        self.users.find_unique.return_value = NS(id='user', passwordHash='stored-hash')
        self.assertEqual(self.client.post('/auth/login', json=self.body).status_code, 401)
        self.verify.return_value = True
        self.assertEqual(self.client.post('/auth/login', json=self.body).json(), {'access_token': 'session-token'})

    def test_db_failure_does_not_return_database_error(self):
        self.users.find_unique.side_effect = RuntimeError('PRIVATE_CONNECTION_STRING')
        response = self.client.post('/auth/register', json=self.body)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('PRIVATE_CONNECTION_STRING', response.text)


class PasswordUtilityTests(unittest.TestCase):
    def test_existing_bcrypt_hashes_and_invalid_hashes(self):
        from src.utils.password import hash_password, verify_password
        hashed = hash_password('password-for-test')
        self.assertTrue(verify_password('password-for-test', hashed))
        self.assertFalse(verify_password('wrong-password', hashed))
        self.assertFalse(verify_password('password-for-test', 'malformed-hash'))
