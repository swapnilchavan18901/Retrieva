"""Exercise public demo login without a database or production credentials."""
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class DemoAuthTests(unittest.TestCase):
    def setUp(self):
        self.users = SimpleNamespace(find_unique=AsyncMock(), upsert=AsyncMock())
        self.sign = Mock(return_value="demo-session-token")
        with patch.dict(sys.modules, {
            "src.db": SimpleNamespace(prisma=SimpleNamespace(user=self.users)),
            "src.utils.jwt": SimpleNamespace(create_access_token=self.sign),
        }):
            module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "src/routes/auth.py"))
        self.demo = SimpleNamespace(id=module["DEMO_USER_ID"], email=module["DEMO_USER_EMAIL"], role="user")
        app = FastAPI()
        app.include_router(module["router"])
        self.client = TestClient(app)

    def test_first_visit_creates_separate_non_admin_account_and_signs_in(self):
        self.users.find_unique.return_value = None
        self.users.upsert.return_value = self.demo
        response = self.client.post("/auth/demo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"access_token": "demo-session-token"})
        creation = self.users.upsert.await_args.kwargs["data"]["create"]
        self.assertEqual(creation["id"], self.demo.id)
        self.assertEqual(creation["role"], "user")
        self.assertTrue(creation["passwordHash"].startswith("$2b$"))
        self.sign.assert_called_once_with({"sub": self.demo.id})

    def test_return_visit_reuses_workspace(self):
        self.users.find_unique.return_value = self.demo
        self.assertEqual(self.client.post("/auth/demo").status_code, 200)
        self.users.upsert.assert_not_awaited()
        self.sign.assert_called_once_with({"sub": self.demo.id})

    def test_reserved_account_cannot_grant_admin_or_other_user_access(self):
        for email, role in ((self.demo.email, "admin"), ("owner@example.com", "user")):
            with self.subTest(email=email, role=role):
                self.users.find_unique.return_value = SimpleNamespace(id=self.demo.id, email=email, role=role)
                self.assertEqual(self.client.post("/auth/demo").status_code, 503)
        self.sign.assert_not_called()

    def test_normal_login_still_requires_credentials(self):
        self.users.find_unique.return_value = None
        response = self.client.post("/auth/login", json={"email": "owner@example.com", "password": "wrong"})
        self.assertEqual(response.status_code, 401)
        self.sign.assert_not_called()


if __name__ == "__main__":
    unittest.main()
