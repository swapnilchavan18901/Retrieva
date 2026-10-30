"""Customer chat keeps reference metadata private and carries conversation history."""
import importlib
import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


def ns(**kwargs):
    return types.SimpleNamespace(**kwargs)


class BusinessLLMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
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


class BusinessSocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
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
        self.assertEqual(len(arguments['context']), 2)
        self.assertEqual(self.db.chatmessage.find_many.await_args.kwargs['where']['id'], {'not': 'current'})
        self.sio.emit.assert_awaited_once_with('assistant_message', {'content': 'We’re open on Sundays from 10 am to 4 pm.'}, to='chat:session')

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
