"""Accept existing HS256 sessions while requiring signed expiry and subject."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import jwt


class JwtTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'test-only-secret-of-at-least-thirty-two-bytes'
        with patch.dict(sys.modules, {'src.env': NS(env=NS(JWT_SECRET=self.secret, ACCESS_TOKEN_EXPIRE_MINUTES=60))}):
            self.module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'src/utils/jwt.py'))

    def test_issued_and_existing_hs256_tokens_are_accepted(self):
        token = self.module['create_access_token']({'sub': 'user'})
        self.assertEqual(self.module['decode_access_token'](token)['sub'], 'user')
        legacy = jwt.encode({'sub': 'user', 'exp': datetime.now(timezone.utc) + timedelta(minutes=5)}, self.secret, algorithm='HS256')
        self.assertEqual(self.module['decode_access_token'](legacy)['sub'], 'user')

    def test_missing_claims_expired_wrong_signature_and_other_algorithms_are_rejected(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=5)
        invalid = [
            jwt.encode({'sub': 'user'}, self.secret, algorithm='HS256'),
            jwt.encode({'exp': future}, self.secret, algorithm='HS256'),
            jwt.encode({'sub': 'user', 'exp': 1}, self.secret, algorithm='HS256'),
            jwt.encode({'sub': 'user', 'exp': future}, 'different-test-key-that-is-long-enough', algorithm='HS256'),
            jwt.encode({'sub': 'user', 'exp': future}, self.secret * 2, algorithm='HS512'),
            'not.a.valid-token',
        ]
        for token in invalid:
            with self.subTest(token=token), self.assertRaises(jwt.InvalidTokenError):
                self.module['decode_access_token'](token)
