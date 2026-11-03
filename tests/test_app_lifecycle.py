"""App-level startup, readiness, error privacy, and production routes."""

from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field


class AppLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = NS(connect=AsyncMock(), disconnect=AsyncMock(), execute_raw=AsyncMock(),
                     is_connected=Mock(return_value=True), query_raw=AsyncMock(return_value=[{'healthy': 1}]))
        modules = {
            'src.db': NS(prisma=self.db),
            'src.env': NS(env=NS(ENVIRONMENT='production', HTTP_REQUESTS_PER_MINUTE=120, CORS_ORIGINS=['https://app.example.com'])),
            'src.sockets.ws_chats': NS(socket_app=FastAPI()),
            'src.producer.producer': NS(publish_document_job=AsyncMock()),
            'src.producer.outbox': NS(run_dispatcher=AsyncMock()),
        }
        for name in ('auth', 'bots', 'api_key', 'documenation', 'testing', 'chats'):
            modules[f'src.routes.{name}'] = NS(router=APIRouter())
        with patch.dict(sys.modules, modules):
            self.app = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))['app']
        # The Socket.IO mount is last; add probes ahead of it.
        self.app.router.routes.pop()

    def test_lifespan_connects_and_disconnects_database(self):
        with TestClient(self.app) as client:
            self.db.connect.assert_awaited_once()
            self.assertEqual(client.get('/health').status_code, 200)
        self.db.disconnect.assert_awaited_once()

    def test_readiness_checks_database_response_and_liveness_stays_available(self):
        with TestClient(self.app) as client:
            self.db.query_raw.side_effect = RuntimeError('offline')
            self.assertEqual(client.get('/health').status_code, 503)
            self.assertEqual(client.get('/').status_code, 200)

    def test_production_api_docs_are_disabled(self):
        with TestClient(self.app) as client:
            for path in ('/docs', '/redoc', '/openapi.json'):
                self.assertEqual(client.get(path).status_code, 404)

    def test_unhandled_error_is_private_and_has_request_id_and_cors(self):
        @self.app.get('/probe')
        async def probe():
            raise RuntimeError('PRIVATE_CONNECTION_STRING')
        with TestClient(self.app, raise_server_exceptions=False) as client:
            response = client.get('/probe', headers={'Origin': 'https://app.example.com'})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('PRIVATE_CONNECTION_STRING', response.text)
        self.assertEqual(response.headers['x-request-id'], response.json()['requestId'])
        self.assertEqual(response.headers['access-control-allow-origin'], 'https://app.example.com')

    def test_validation_errors_do_not_echo_password_input(self):
        class SecretRequest(BaseModel):
            password: str = Field(min_length=20)
        @self.app.post('/probe')
        async def probe(data: SecretRequest):
            return {}
        with TestClient(self.app) as client:
            response = client.post('/probe', json={'password': 'PRIVATE_INPUT'})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('PRIVATE_INPUT', response.text)
        self.assertNotIn('input', response.json()['detail'][0])
