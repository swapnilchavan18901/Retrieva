"""Check AWS credential precedence without reading real credentials or using AWS."""

import os
from pathlib import Path
import runpy
import shutil
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILES = ("src/env.py", "consumer/consumer_env.py")
VALUES = {
    "DATABASE_URL": "postgresql://test:test@localhost/test",
    "JWT_SECRET": "test-only",
    "AWS_REGION": "us-east-1",
    "AWS_S3_BUCKET": "test-bucket",
    "AWS_ACCESS_KEY_ID": "file-access-key",
    "AWS_SECRET_ACCESS_KEY": "file-secret-key",
    "RABBIT_MQ_URL": "amqp://localhost",
    "RABBIT_MQ_API_KEY": "test-only",
    "SARVAM_API_KEY": "test-only",
    "OPENROUTER_API_KEY": "test-only",
    "QDRANT_DB_CLUSTER_URL": "http://localhost:6333",
    "QDRANT_DB_API_KEY": "test-only",
}


class AWSSettingsTests(unittest.TestCase):
    def load_settings(self, relative_path, values):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / relative_path
            target.parent.mkdir()
            shutil.copyfile(ROOT / relative_path, target)
            (root / ".env").write_text("\n".join(f"{key}={value}" for key, value in values.items()))
            with patch.dict(os.environ, {
                "AWS_ACCESS_KEY_ID": "shell-access-key",
                "AWS_SECRET_ACCESS_KEY": "shell-secret-key",
                "AWS_SESSION_TOKEN": "shell-token",
                "AWS_REGION": "eu-west-1",
                "AWS_S3_BUCKET": "shell-bucket",
                "JWT_SECRET": "shell-jwt",
            }, clear=True):
                return runpy.run_path(str(target))["env"]

    def test_both_services_use_file_aws_settings(self):
        for path in SETTINGS_FILES:
            with self.subTest(path=path):
                settings = self.load_settings(path, VALUES)
                for key in VALUES:
                    if key.startswith("AWS_"):
                        self.assertEqual(getattr(settings, key), VALUES[key])
                self.assertIsNone(settings.AWS_SESSION_TOKEN)
                self.assertEqual(settings.JWT_SECRET, "shell-jwt")

    def test_temporary_credentials_use_file_session_token(self):
        for path in SETTINGS_FILES:
            with self.subTest(path=path):
                settings = self.load_settings(path, {**VALUES, "AWS_SESSION_TOKEN": "file-token"})
                self.assertEqual(settings.AWS_SESSION_TOKEN, "file-token")

    def test_missing_or_empty_file_credentials_do_not_fall_back(self):
        for path in SETTINGS_FILES:
            for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
                for empty in (False, True):
                    with self.subTest(path=path, key=key, empty=empty):
                        values = dict(VALUES)
                        if empty:
                            values[key] = ""
                        else:
                            del values[key]
                        with self.assertRaises(ValidationError):
                            self.load_settings(path, values)


if __name__ == "__main__":
    unittest.main()
