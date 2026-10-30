"""Offline revision tests: exact reuse, failures, visibility, metadata and isolation."""
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from shared import benchmark

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
from shared.chunking import structure_chunks

VECTOR = [1.0] + [0.0] * 1535


class StructureChunkingTests(unittest.TestCase):
    def test_local_paragraph_edit_does_not_change_other_chunks(self):
        old = structure_chunks('# Leave policy\n\nOld rule.\n\n# Salary policy\n\nUnchanged salary.\n\nUnchanged benefits.')
        new = structure_chunks('# Leave policy\n\nNew and much longer rule.\n\n# Salary policy\n\nUnchanged salary.\n\nUnchanged benefits.')
        self.assertEqual(old[2:], new[2:])
        self.assertEqual(old[3]['section'], '# Salary policy')

    def test_unwraps_pdf_lines_and_normalizes_whitespace(self):
        self.assertEqual(structure_chunks('Hello   world\ncontinued here.\r\n\r\nAnother paragraph.'),
                         [{'text': 'Hello world continued here.', 'section': None}, {'text': 'Another paragraph.', 'section': None}])

    def test_long_blocks_stay_bounded_without_cross_paragraph_overlap(self):
        chunks = structure_chunks('word ' * 600 + '\n\nIndependent paragraph.', maximum=100)
        self.assertTrue(all(0 < len(chunk['text']) <= 100 for chunk in chunks))
        self.assertEqual(chunks[-1]['text'], 'Independent paragraph.')
        self.assertEqual(' '.join(chunk['text'] for chunk in chunks[:-1]), ('word ' * 600).strip())
        self.assertEqual(structure_chunks('x' * 205, maximum=100)[-1]['text'], 'x' * 5)
        self.assertEqual(structure_chunks(' \n\n '), [])


class IncrementalIndexingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        benchmark_patch = patch.object(benchmark, "emit_event")
        benchmark_patch.start()
        self.addCleanup(benchmark_patch.stop)
        self.db = QdrantClient(':memory:')
        self.documents = []
        self.prisma = types.SimpleNamespace(document=types.SimpleNamespace(find_many=AsyncMock(side_effect=lambda **kwargs: self.documents)))
        modules = patch.dict(sys.modules, {
            'consumer.vector.qdrantdb': types.SimpleNamespace(qdrant_client=self.db),
            'src.vector.client': types.SimpleNamespace(qdrant_client=self.db),
            'src.db': types.SimpleNamespace(prisma=self.prisma),
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.addCleanup(self.db.close)
        importlib.reload(importlib.import_module('consumer.vector.vector'))
        self.index = importlib.reload(importlib.import_module('consumer.vector.insert'))
        self.search = importlib.reload(importlib.import_module('src.vector.retrieve'))
        self.embed = AsyncMock(side_effect=lambda texts: [VECTOR for _ in texts])
        patches = [patch.object(self.index, 'embed_texts_async', self.embed),
                   patch.object(self.search, 'embed_text', AsyncMock(return_value=VECTOR))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def docs(self, texts, page=1):
        return [types.SimpleNamespace(page_content='\n\n'.join(texts), metadata={'page': page})]

    async def stage(self, texts, revision, doc_id='doc', page=1, name='policy.pdf', legacy=False):
        return await self.index.embed_and_store(self.docs(texts, page), 'bot', doc_id, name, 'user', revision, legacy)

    async def activate(self, revision, doc_id='doc'):
        existing = next((doc for doc in self.documents if doc.id == doc_id), None)
        if existing:
            existing.activeRevision = revision
        else:
            self.documents.append(types.SimpleNamespace(id=doc_id, activeRevision=revision))
        await self.index.cleanup_revision('bot', doc_id, 'user', revision)

    async def visible(self):
        return await self.search.retrieve_relevant_chunks('bot', 'question', top_k=100)

    async def points(self, doc_id='doc'):
        return await self.index.document_points('bot', doc_id, 'user')

    async def test_a_to_l_only_embeds_l_and_keeps_four_point_ids(self):
        old = ['A paragraph.', 'B paragraph.', 'C paragraph.', 'D paragraph.', 'E paragraph.']
        await self.stage(old, 'v1')
        await self.activate('v1')
        before = {p.payload['text']: str(p.id) for p in await self.points()}
        self.embed.reset_mock()
        stats = await self.stage(['L paragraph.', *old[1:]], 'v2')
        self.embed.assert_awaited_once_with(['L paragraph.'])
        self.assertEqual(stats, {'total': 5, 'embedded': 1, 'reused': 4, 'removed': 1})
        self.assertEqual({p['text'] for p in await self.visible()}, set(old))
        await self.activate('v2')
        after = {p.payload['text']: str(p.id) for p in await self.points()}
        self.assertNotIn('A paragraph.', after)
        for text in old[1:]:
            self.assertEqual(before[text], after[text])
        self.assertEqual({p['text'] for p in await self.visible()}, {'L paragraph.', *old[1:]})

    async def test_identical_retry_and_reorder_make_no_embedding_call(self):
        await self.stage(['A paragraph.', 'B paragraph.'], 'v1')
        await self.activate('v1')
        self.embed.reset_mock()
        await self.stage(['B paragraph.', 'A paragraph.'], 'v2')
        self.embed.assert_not_awaited()
        await self.activate('v2')
        await self.stage(['B paragraph.', 'A paragraph.'], 'v2')
        self.embed.assert_not_awaited()
        self.assertEqual(len(await self.points()), 2)

    async def test_embedding_failure_leaves_previous_version_untouched(self):
        await self.stage(['A paragraph.', 'B paragraph.'], 'v1')
        await self.activate('v1')
        before = [(p.id, p.payload, p.vector) for p in await self.points()]
        self.embed.side_effect = RuntimeError('provider unavailable')
        with self.assertRaises(RuntimeError):
            await self.stage(['L paragraph.', 'B paragraph.'], 'v2')
        self.assertEqual([(p.id, p.payload, p.vector) for p in await self.points()], before)
        self.assertEqual({p['text'] for p in await self.visible()}, {'A paragraph.', 'B paragraph.'})

    async def test_partial_write_is_hidden_and_retry_reuses_staged_embedding(self):
        await self.stage(['A paragraph.', 'B paragraph.'], 'v1')
        await self.activate('v1')
        real_upsert = self.db.upsert
        def write_then_fail(*args, **kwargs):
            real_upsert(*args, **kwargs)
            raise RuntimeError('response lost after write')
        with patch.object(self.db, 'upsert', side_effect=write_then_fail):
            with self.assertRaises(RuntimeError):
                await self.stage(['L paragraph.', 'B paragraph.'], 'v2')
        self.assertEqual({p['text'] for p in await self.visible()}, {'A paragraph.', 'B paragraph.'})
        self.embed.reset_mock()
        await self.stage(['L paragraph.', 'B paragraph.'], 'v2')
        self.embed.assert_not_awaited()
        await self.activate('v2')
        self.assertEqual(len(await self.points()), 2)

    async def test_page_and_filename_changes_reuse_vectors_with_versioned_metadata(self):
        await self.stage(['B paragraph.'], 'v1', page=1)
        await self.activate('v1')
        point_id = (await self.points())[0].id
        self.embed.reset_mock()
        await self.stage(['B paragraph.'], 'v2', page=5, name='renamed.pdf')
        self.assertEqual((await self.visible())[0]['page'], 1)
        await self.activate('v2')
        result = (await self.visible())[0]
        self.assertEqual((result['page'], result['file_name']), (5, 'renamed.pdf'))
        self.assertEqual((await self.points())[0].id, point_id)
        self.embed.assert_not_awaited()

    async def test_repeated_chunks_keep_occurrences_and_cleanup_is_document_scoped(self):
        await self.stage(['Repeated.', 'Repeated.'], 'v1')
        await self.activate('v1')
        self.assertEqual(len(await self.points()), 2)
        self.assertEqual(self.embed.await_args.args[0], ['Repeated.'])
        await self.stage(['Other document.'], 'other-v1', doc_id='other')
        await self.activate('other-v1', doc_id='other')
        await self.stage(['Repeated.'], 'v2')
        await self.activate('v2')
        self.assertEqual(len(await self.points()), 1)
        self.assertEqual(len(await self.points('other')), 1)

    async def test_legacy_points_remain_visible_until_activation(self):
        await self.index.ensure_collection('bot')
        self.db.upsert('bot', [PointStruct(id=1, vector=VECTOR, payload={
            'doc_id': 'doc', 'user_id': 'user', 'text': 'Unchanged.', 'page': 2, 'file_name': 'old.pdf',
        }), PointStruct(id=2, vector=VECTOR, payload={
            'doc_id': 'doc', 'user_id': 'user', 'text': 'Old.', 'page': 2, 'file_name': 'old.pdf',
        })])
        self.documents = [types.SimpleNamespace(id='doc', activeRevision=None)]
        await self.stage(['Unchanged.', 'New.'], 'v2', legacy=True)
        self.assertEqual(self.embed.await_args.args[0], ['New.'])
        self.assertEqual({p['text'] for p in await self.visible()}, {'Unchanged.', 'Old.'})
        await self.activate('v2')
        self.assertEqual({p['text'] for p in await self.visible()}, {'Unchanged.', 'New.'})
        self.assertIn(1, [p.id for p in await self.points()])

    async def test_empty_document_preserves_existing_points(self):
        await self.stage(['Keep this.'], 'v1')
        await self.activate('v1')
        with self.assertRaisesRegex(ValueError, 'no extractable text'):
            await self.stage(['  '], 'v2')
        self.assertEqual(len(await self.points()), 1)


if __name__ == '__main__':
    unittest.main()
