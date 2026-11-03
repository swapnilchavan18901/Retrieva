"""Worker PDFs are bounded independently of client-declared upload sizes."""

import io
from pathlib import Path
import runpy
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


class PdfLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.s3 = Mock()
        self.data = b'%PDF-1.7\nplaceholder\n'
        self.stream = io.BytesIO(self.data)
        self.s3.get_object.return_value = {'ContentLength': len(self.data), 'Body': self.stream}
        self.reader = NS(is_encrypted=False, pages=[NS(extract_text=Mock(return_value='A page of text'))])
        with patch('boto3.client', return_value=self.s3), patch.dict(sys.modules, {
            'consumer.consumer_env': NS(env=NS(AWS_REGION='us-east-1', AWS_ACCESS_KEY_ID='test',
                AWS_SECRET_ACCESS_KEY='test', AWS_SESSION_TOKEN=None, AWS_S3_BUCKET='test-bucket')),
        }):
            self.module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'consumer/aws/file_loader.py'))
        self.load = self.module['_load_pdf_from_s3_sync']
        self.load.__globals__['PdfReader'] = Mock(return_value=self.reader)
        original = tempfile.NamedTemporaryFile
        temporary_patch = patch('tempfile.NamedTemporaryFile', side_effect=lambda **kwargs: original(dir=self.directory.name, **kwargs))
        temporary_patch.start()
        self.addCleanup(temporary_patch.stop)

    def assert_failed_and_cleaned(self):
        with self.assertRaises(ValueError):
            self.load('document.pdf')
        self.assertTrue(self.stream.closed)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_valid_pdf_returns_page_metadata_and_closes_s3_stream(self):
        docs, path = self.load('document.pdf')
        self.assertEqual(docs[0].page_content, 'A page of text')
        self.assertEqual(docs[0].metadata, {'page': 1, 'source': 'document.pdf', 'total_pages': 1})
        self.assertTrue(Path(path).exists())
        self.assertTrue(self.stream.closed)

    def test_oversized_download_rejected_before_creating_temp_file(self):
        self.s3.get_object.return_value['ContentLength'] = self.module['MAX_FILE_BYTES'] + 1
        self.assert_failed_and_cleaned()

    def test_stream_cannot_exceed_limit_even_if_content_length_lies(self):
        self.load.__globals__['MAX_FILE_BYTES'] = len(self.data) - 1
        self.s3.get_object.return_value['ContentLength'] = 1
        self.assert_failed_and_cleaned()

    def test_truncated_download_is_rejected(self):
        self.s3.get_object.return_value['ContentLength'] += 1
        self.assert_failed_and_cleaned()

    def test_non_pdf_is_rejected(self):
        self.stream = io.BytesIO(b'not a PDF')
        self.s3.get_object.return_value = {'ContentLength': 9, 'Body': self.stream}
        self.assert_failed_and_cleaned()

    def test_encrypted_pdf_is_rejected(self):
        self.reader.is_encrypted = True
        self.assert_failed_and_cleaned()

    def test_page_limit_is_enforced_before_extracting_text(self):
        self.reader.pages *= 501
        self.assert_failed_and_cleaned()
        self.reader.pages[0].extract_text.assert_not_called()

    def test_empty_or_scanned_pdf_is_rejected(self):
        self.reader.pages[0].extract_text.return_value = '  '
        self.assert_failed_and_cleaned()

    def test_text_limit_is_enforced(self):
        self.load.__globals__['MAX_EXTRACTED_CHARACTERS'] = 5
        self.assert_failed_and_cleaned()
