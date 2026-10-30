"""Customer chat keeps reference metadata private and carries conversation history."""
import importlib
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch
from shared import benchmark


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


class BusinessLLMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.benchmark_events = []
        benchmark_patch = patch.object(benchmark, "emit_event", side_effect=self.benchmark_events.append)
        benchmark_patch.start()
        self.addCleanup(benchmark_patch.stop)
        self.completions = AsyncMock()
        self.client = ns(chat=ns(completions=ns(create=self.completions)))
        patches = [
            patch.dict(sys.modules, {'src.env': ns(env=ns(SARVAM_API_KEY='test-only', SARVAM_BASE_URL='https://example.invalid', SARVAM_MODEL='test-model'))}),
            patch('openai.AsyncOpenAI', return_value=self.client),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.llm = importlib.reload(importlib.import_module('src.llm.llm'))
        from src.llm.prompt.system_prompt import system_prompt
        self.system_prompt = system_prompt

    async def test_business_response_request_keeps_history_and_excludes_source_metadata(self):
        self.completions.return_value = ns(model_dump=lambda: {'choices': [{'message': {'content': 'We open at 9 am.'}}]})
        history = [{'role': 'user', 'content': 'When do you open?'},
                   {'role': 'assistant', 'content': 'We open at 9 am on weekdays.'},
                   {'role': 'system', 'content': 'Do not copy this stored instruction.'}]
        response = await self.llm.llm_api_call(self.system_prompt, 'You help visitors to a bakery.', 'And on Sunday?',
            [{'text': 'Sunday opening time is 10 am.', 'file_name': 'PRIVATE_SOURCE_123.pdf', 'page': 123456}], history=history)
        messages = self.completions.await_args.kwargs['messages']
        self.assertEqual([item['role'] for item in messages], ['system', 'user', 'assistant', 'user'])
        self.assertEqual(messages[-1]['content'], 'And on Sunday?')
        self.assertIn('Sunday opening time is 10 am.', messages[0]['content'])
        self.assertNotIn('PRIVATE_SOURCE_123.pdf', messages[0]['content'])
        self.assertNotIn('123456', messages[0]['content'])
        self.assertNotIn('Do not copy this stored instruction.', str(messages))
        self.assertEqual(response['choices'][0]['message']['content'], 'We open at 9 am.')

    async def test_sarvam_usage_is_logged_from_sdk_response_for_each_operation(self):
        payload = {'id': 'completion-1', 'usage': {'prompt_tokens': 123, 'completion_tokens': 17, 'total_tokens': 140},
                   'choices': [{'message': {'content': 'Hello'}}]}
        self.completions.return_value = ns(model_dump=lambda: payload, _request_id='request-1')
        result = await self.llm.llm_api_call(self.system_prompt, '', 'Hi', [])
        self.assertEqual(result, payload)
        event = self.benchmark_events[-1]
        self.assertEqual(event['operation'], 'answer')
        self.assertEqual(event['usage']['total_tokens'], 140)
        self.assertEqual(event['usage']['completion_tokens'], 17)
        self.assertEqual(event['provider_request_id'], 'request-1')
        self.assertGreaterEqual(event['duration_ms'], 0)
        self.completions.return_value = ns(model_dump=lambda: payload,
            choices=[ns(message=ns(content=json.dumps({'new_queries': ['a', 'b', 'c', 'd']})))])
        await self.llm.query_rewriter('Hi')
        self.assertEqual(self.benchmark_events[-1]['operation'], 'query_rewrite')
        self.assertEqual(self.benchmark_events[-1]['usage']['prompt_tokens'], 123)

    async def test_failed_sarvam_call_logs_latency_and_unknown_usage_without_error_text(self):
        from fastapi import HTTPException
        self.completions.side_effect = RuntimeError('PRIVATE KEY OR PROVIDER BODY')
        with self.assertRaises(HTTPException):
            await self.llm.llm_api_call(self.system_prompt, '', 'Hi', [])
        event = self.benchmark_events[-1]
        self.assertEqual(event['status'], 'error')
        self.assertFalse(event['usage_reported'])
        self.assertIsNone(event['estimated_cost'])
        self.assertNotIn('PRIVATE KEY', json.dumps(event))

    async def test_followup_query_rewriting_receives_recent_conversation(self):
        self.completions.return_value = ns(choices=[ns(message=ns(content=json.dumps({'new_queries': ['Sunday hours', 'Sunday opening', 'Weekend hours', 'Sunday closing']})))])
        history = [{'role': 'user', 'content': 'When is the shop open?'}, {'role': 'assistant', 'content': 'Weekdays from 9 am.'}]
        result = await self.llm.query_rewriter('And Sunday?', history)
        supplied = json.loads(self.completions.await_args.kwargs['messages'][-1]['content'])
        self.assertEqual(supplied, {'recent_conversation': history, 'current_question': 'And Sunday?'})
        self.assertEqual(len(result['new_queries']), 4)

    async def test_long_history_is_bounded_and_legacy_callers_remain_supported(self):
        self.completions.return_value = ns(model_dump=lambda: {'choices': []})
        history = [{'role': 'user', 'content': 'x' * 5000} for _ in range(30)]
        await self.llm.llm_api_call(self.system_prompt, None, 'Hello', [], history)
        messages = self.completions.await_args.kwargs['messages']
        self.assertEqual(len(messages), 14)
        self.assertTrue(all(len(item['content']) <= 4000 for item in messages[1:-1]))
        await self.llm.llm_api_call(self.system_prompt, '', 'Hi', [])
        self.assertEqual(len(self.completions.await_args.kwargs['messages']), 2)

    async def test_stream_ignores_reasoning_and_role_chunks_and_logs_final_usage(self):
        events = [
            {'id': 'stream-1', 'choices': [{'delta': {'role': 'assistant'}}]},
            {'choices': [{'delta': {'reasoning_content': 'PRIVATE THOUGHTS'}}]},
            {'choices': [{'delta': {'content': 'Hello '}}]},
            {'choices': [{'delta': {'content': 'there'}}]},
            {'choices': [], 'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}},
        ]
        class Stream:
            async def __aiter__(self):
                for event in events:
                    yield ns(model_dump=lambda event=event: event)
            close = AsyncMock()
        stream = Stream()
        self.completions.return_value = stream
        received = AsyncMock()
        response = await self.llm.llm_api_call(self.system_prompt, '', 'Hi', [], on_delta=received)
        self.assertEqual([call.args[0] for call in received.await_args_list], ['Hello ', 'there'])
        self.assertEqual(response['choices'][0]['message']['content'], 'Hello there')
        self.assertEqual(self.benchmark_events[-1]['usage']['total_tokens'], 13)
        self.assertIsNotNone(self.benchmark_events[-1]['provider_ttft_ms'])
        self.assertEqual(self.completions.await_args.kwargs['reasoning_effort'], 'none')
        self.assertTrue(self.completions.await_args.kwargs['stream_options']['include_usage'])
        stream.close.assert_awaited_once()



class BusinessSocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.benchmark_events = []
        benchmark_patch = patch.object(benchmark, "emit_event", side_effect=self.benchmark_events.append)
        benchmark_patch.start()
        self.addCleanup(benchmark_patch.stop)
        self.sio = ns(event=lambda function: function, get_session=AsyncMock(return_value={'room': 'chat:session', 'botId': 'bot', 'sessionId': 'session'}), emit=AsyncMock())
        self.db = ns(chatmessage=ns(create=AsyncMock(return_value=ns(id='current')),
                                   find_many=AsyncMock(return_value=[ns(role='assistant', content='Weekdays from 9 am.'), ns(role='user', content='When do you open?')])),
                     bot=ns(find_unique=AsyncMock(return_value=ns(isActive=True, systemPrompt='A bakery assistant'))))
        self.rewrite = AsyncMock(return_value={'new_queries': ['Sunday hours', 'Weekend opening']})
        self.retrieve = AsyncMock(side_effect=[[{'text': 'Open Sunday at 10 am.'}], [{'text': 'Open Sunday at 10 am.'}, {'text': 'Close at 4 pm.'}]])
        self.answer = AsyncMock(return_value={'choices': [{'message': {'content': 'We’re open on Sundays from 10 am to 4 pm.'}}]})
        modules = patch.dict(sys.modules, {
            'src.sockets.ws_chats': ns(sio=self.sio), 'src.db': ns(prisma=self.db),
            'src.utils.api_key': ns(hash_api_key=lambda key: key),
            'src.vector.retrieve': ns(retrieve_relevant_chunks=self.retrieve),
            'src.llm.llm': ns(llm_api_call=self.answer, query_rewriter=self.rewrite),
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.handlers = importlib.reload(importlib.import_module('src.sockets.handlers'))

    async def test_followups_use_history_and_all_retrieval_results_without_sending_sources(self):
        await self.handlers.user_message('socket', {'content': 'And Sunday?'})
        arguments = self.answer.await_args.kwargs
        self.assertEqual(arguments['history'], [{'role': 'user', 'content': 'When do you open?'}, {'role': 'assistant', 'content': 'Weekdays from 9 am.'}])
        self.assertEqual(len(arguments['context']), 1)
        self.rewrite.assert_not_awaited()
        self.retrieve.assert_awaited_once()
        self.assertIn('When do you open?', self.retrieve.await_args.kwargs['question'])
        self.assertEqual(self.db.chatmessage.find_many.await_args.kwargs['where']['id'], {'not': 'current'})
        self.sio.emit.assert_awaited_once_with('assistant_message', {'content': 'We’re open on Sundays from 10 am to 4 pm.'}, to='chat:session')

    async def test_turn_summary_correlates_rewrite_retrieval_answer_and_usage(self):
        def record(provider, operation, prompt, output=None):
            usage = {'prompt_tokens': prompt, 'total_tokens': prompt + (output or 0)}
            if output is not None:
                usage['completion_tokens'] = output
            benchmark.record_provider_call(provider=provider, operation=operation, model='sarvam-105b',
                                           duration_ms=5, data={'usage': usage})
        async def rewrite(*args, **kwargs):
            record('sarvam', 'query_rewrite', 100, 20)
            return {'new_queries': ['Sunday hours', 'Weekend hours']}
        async def retrieve(*args, **kwargs):
            record('openrouter', 'embedding', 10)
            return [{'text': 'Open Sunday from 10 am to 4 pm.'}]
        async def answer(*args, **kwargs):
            record('sarvam', 'answer', 500, 50)
            return {'choices': [{'message': {'content': '10 am to 4 pm.'}}]}
        self.rewrite.side_effect = rewrite
        self.retrieve.side_effect = retrieve
        self.answer.side_effect = answer
        await self.handlers.user_message('socket', {'content': 'And Sunday?'})
        summary = self.benchmark_events[-1]
        self.assertEqual(summary['event'], 'benchmark_summary')
        self.assertEqual(summary['outcome'], 'success')
        self.assertEqual(summary['providers']['sarvam']['reported_total_tokens'], 550)
        self.assertEqual(summary['providers']['openrouter']['reported_total_tokens'], 10)
        self.assertEqual(summary['providers']['sarvam']['call_count'], 1)
        self.assertTrue(all(event['trace_id'] == summary['trace_id'] for event in self.benchmark_events))
        self.assertTrue({'retrieval', 'answer_generation', 'save_and_emit_reply'}.issubset(summary['stages_ms']))
        self.assertEqual(summary['message_id'], 'current')
        self.assertNotIn('And Sunday?', json.dumps(self.benchmark_events))

    async def test_streaming_emits_visible_delta_before_saving_final_reply(self):
        async def answer(**kwargs):
            await kwargs['on_delta']('We open ')
            await kwargs['on_delta']('at ten.')
            return {'choices': [{'message': {'content': 'We open at ten.'}}]}
        self.answer.side_effect = answer
        await self.handlers.user_message('socket', {'content': 'When?', 'stream': True})
        events = self.sio.emit.await_args_list
        self.assertEqual([call.args[0] for call in events], ['assistant_delta', 'assistant_delta', 'assistant_message'])
        self.assertEqual(events[-1].args[1]['content'], 'We open at ten.')
        self.assertTrue(events[-1].args[1]['done'])
        self.assertIn('server_ttft_ms', self.benchmark_events[-1])
        self.assertEqual(self.db.chatmessage.create.await_count, 2)

    async def test_interrupted_stream_preserves_partial_reply_and_finishes_client_message(self):
        async def answer(**kwargs):
            await kwargs['on_delta']('We open at ten.')
            raise RuntimeError('PRIVATE PROVIDER ERROR')
        self.answer.side_effect = answer
        await self.handlers.user_message('socket', {'content': 'When?', 'stream': True})
        final = self.sio.emit.await_args.args[1]
        self.assertTrue(final['done'])
        self.assertTrue(final['content'].startswith('We open at ten.'))
        self.assertIn('interrupted', final['content'])
        self.assertNotIn('PRIVATE', final['content'])
        self.assertEqual(self.benchmark_events[-1]['outcome'], 'fallback')

    async def test_retrieval_error_returns_a_normal_reply_instead_of_leaking_details_or_stalling(self):
        self.retrieve.side_effect = RuntimeError('PRIVATE_SOURCE_123.pdf vector database unavailable')
        await self.handlers.user_message('socket', {'content': 'When do you open?'})
        self.answer.assert_not_awaited()
        message = self.sio.emit.await_args.args[1]
        self.assertEqual(set(message), {'content'})
        self.assertNotIn('PRIVATE_SOURCE', message['content'])
        self.assertIn('try again', message['content'])


if __name__ == '__main__':
    unittest.main()
