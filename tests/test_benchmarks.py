"""Provider usage, cost math and trace logging tests; no paid API calls."""
import asyncio
from decimal import Decimal
import io
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from shared import benchmark


class BenchmarkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.original_emit = benchmark.emit_event
        self.config = benchmark.BenchmarkSettings(_env_file=None, BENCHMARK_ENABLED=True, BENCHMARK_LOG_TO_CONSOLE=False,
            BENCHMARK_LOG_PATH='', SARVAM_INPUT_PRICE_PER_1M_TOKENS=Decimal('15'),
            SARVAM_CACHED_INPUT_PRICE_PER_1M_TOKENS=Decimal('5'), SARVAM_OUTPUT_PRICE_PER_1M_TOKENS=Decimal('60'))
        for item in (patch.object(benchmark, 'settings', return_value=self.config),
                     patch.object(benchmark, 'emit_event', side_effect=self.events.append)):
            item.start()
            self.addCleanup(item.stop)

    def call(self, **overrides):
        arguments = dict(provider='sarvam', operation='answer', model='sarvam-105b', duration_ms=2000,
            data={'id': 'completion-1', 'model': 'sarvam-105b', 'usage': {
                'prompt_tokens': 1000, 'completion_tokens': 100, 'total_tokens': 1100,
                'prompt_tokens_details': {'cached_tokens': 200},
                'completion_tokens_details': {'reasoning_tokens': 20}},
                'choices': [{'message': {'content': 'PRIVATE CUSTOMER MESSAGE'}}]})
        arguments.update(overrides)
        benchmark.record_provider_call(**arguments)
        return self.events[-1]

    def test_reported_counts_and_cached_cost_without_double_counting_reasoning(self):
        event = self.call(provider_request_id='provider-123')
        self.assertEqual(event['usage'], {'prompt_tokens': 1000, 'completion_tokens': 100,
            'total_tokens': 1100, 'cached_prompt_tokens': 200, 'reasoning_tokens': 20})
        self.assertEqual(Decimal(event['estimated_cost']['amount']), Decimal('0.019'))
        self.assertEqual(event['estimated_cost']['kind'], 'estimate')
        self.assertIsNone(event['reported_cost'])
        self.assertEqual(event['effective_output_tokens_per_second'], 50)
        self.assertEqual(event['provider_request_id'], 'provider-123')
        self.assertNotIn('PRIVATE CUSTOMER MESSAGE', json.dumps(event))

    def test_missing_usage_is_null_never_tokenized_or_inferred(self):
        event = self.call(data={'choices': [{'message': {'content': 'some very long text ' * 1000}}]})
        self.assertTrue(all(value is None for value in event['usage'].values()))
        self.assertFalse(event['usage_reported'])
        self.assertFalse(event['usage_complete'])
        self.assertIsNone(event['estimated_cost'])
        event = self.call(data={'usage': {'prompt_tokens': 7, 'completion_tokens': 2}})
        self.assertIsNone(event['usage']['total_tokens'])
        self.assertFalse(event['usage_complete'])

    def test_unknown_cache_is_explicit_and_invalid_or_unknown_pricing_is_not_guessed(self):
        event = self.call(data={'usage': {'prompt_tokens': 1000, 'completion_tokens': 100, 'total_tokens': 1100}})
        self.assertIsNone(event['usage']['cached_prompt_tokens'])
        self.assertEqual(event['estimated_cost']['cache_assumption'], 'all_input_uncached')
        self.assertEqual(Decimal(event['estimated_cost']['amount']), Decimal('0.021'))
        self.assertIsNone(self.call(model='new-unpriced-model')['estimated_cost'])
        self.assertIsNone(self.call(data={'usage': {'prompt_tokens': 1, 'completion_tokens': 1,
            'prompt_tokens_details': {'cached_tokens': 2}}})['estimated_cost'])

    def test_custom_rates_apply_without_changing_reported_token_counts(self):
        self.config.SARVAM_INPUT_PRICE_PER_1M_TOKENS = Decimal('42')
        self.config.SARVAM_CACHED_INPUT_PRICE_PER_1M_TOKENS = Decimal('7')
        self.config.SARVAM_OUTPUT_PRICE_PER_1M_TOKENS = Decimal('99')
        self.assertEqual(Decimal(self.call()['estimated_cost']['amount']), Decimal('0.0449'))

    def test_openrouter_preserves_reported_cost_separately_in_usd(self):
        event = self.call(provider='openrouter', operation='embedding', model='openai/text-embedding-3-small',
            data={'usage': {'prompt_tokens': 99, 'total_tokens': 99, 'cost': 0.00000198}})
        self.assertEqual(Decimal(event['reported_cost']['amount']), Decimal('0.00000198'))
        self.assertEqual(event['reported_cost']['currency'], 'USD')
        self.assertIsNone(event['estimated_cost'])
        self.assertIsNone(event['usage']['completion_tokens'])
        self.assertTrue(event['usage_complete'])

    def test_errors_and_missing_fields_do_not_become_zero_cost_successes(self):
        with benchmark.benchmark_run('chat_turn'):
            self.call()
            self.call(data={}, status='error', http_status=402, error_type='APIStatusError')
        summary = self.events[-1]
        totals = summary['providers']['sarvam']
        self.assertEqual(totals['call_count'], 2)
        self.assertEqual(totals['error_count'], 1)
        self.assertEqual(totals['usage_missing_calls'], 1)
        self.assertFalse(totals['usage_complete'])
        self.assertEqual(totals['reported_total_tokens'], 1100)
        self.assertEqual(totals['estimated_cost_calls'], 1)

    def test_all_missing_usage_stays_null_in_summary_instead_of_claiming_zero(self):
        with benchmark.benchmark_run('chat_turn'):
            self.call(data={}, status='error')
        totals = self.events[-1]['providers']['sarvam']
        self.assertIsNone(totals['reported_total_tokens'])
        self.assertIsNone(totals['reported_prompt_tokens'])
        self.assertIsNone(totals['estimated_cost_inr'])
        self.assertIsNone(totals['reported_cost_usd'])
        self.assertFalse(totals['usage_complete'])

    async def test_parallel_requests_keep_isolated_traces_and_collect_child_calls(self):
        async def run(identifier):
            with benchmark.benchmark_run('chat_turn', session_id=identifier) as span:
                with span.stage('retrieval'):
                    async def child():
                        await asyncio.sleep(0)
                        self.call()
                    await asyncio.gather(child(), child())
        await asyncio.gather(run('session-a'), run('session-b'))
        summaries = [event for event in self.events if event['event'] == 'benchmark_summary']
        self.assertEqual(len({event['trace_id'] for event in summaries}), 2)
        for summary in summaries:
            calls = [event for event in self.events if event['event'] == 'provider_call' and event['trace_id'] == summary['trace_id']]
            self.assertEqual(len(calls), 2)
            self.assertTrue(all(event['session_id'] == summary['session_id'] for event in calls))
            self.assertEqual(summary['providers']['sarvam']['reported_total_tokens'], 2200)
            self.assertGreaterEqual(summary['server_total_ms'], summary['stages_ms']['retrieval'])
        self.assertIsNone(self.call()['trace_id'])

    def test_pipeline_fallback_and_exceptions_are_reported_without_exception_text(self):
        with benchmark.benchmark_run('chat_turn') as span:
            span.failed(ValueError('PRIVATE PROMPT'), outcome='fallback')
        self.assertEqual(self.events[-1]['outcome'], 'fallback')
        with self.assertRaises(RuntimeError):
            with benchmark.benchmark_run('document_index'):
                raise RuntimeError('SECRET KEY')
        event = self.events[-1]
        self.assertEqual(event['error_type'], 'RuntimeError')
        self.assertEqual(event['outcome'], 'error')
        self.assertNotIn('SECRET KEY', json.dumps(event))
        self.assertIsNone(benchmark._current_run.get())

    def test_malformed_usage_does_not_break_logging(self):
        for data in ({'usage': []}, {'usage': {'prompt_tokens': True, 'completion_tokens': -4}},
                     {'usage': {'prompt_tokens_details': 'wrong shape'}}, {}):
            event = self.call(data=data)
            self.assertFalse(event['usage_complete'])
            self.assertIsNone(event['estimated_cost'])
        self.assertIsNone(self.call(provider='openrouter', data={'usage': {'cost': 'NaN'}})['reported_cost'])

    def test_logging_failure_does_not_break_provider_or_pipeline(self):
        with patch.object(benchmark, 'emit_event', side_effect=OSError('disk full')):
            with benchmark.benchmark_run('chat_turn'):
                benchmark.record_provider_call(provider='sarvam', operation='answer', model='sarvam-105b', duration_ms=1)
        self.assertIsNone(benchmark._current_run.get())

    def test_jsonl_sink_and_disable_switch(self):
        logger = logging.getLogger(f'retrieva.benchmark.{__import__("os").getpid()}')
        saved_handlers = list(logger.handlers)
        logger.handlers.clear()
        benchmark._logger.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'benchmarks-{pid}.jsonl'
                self.config.BENCHMARK_LOG_PATH = str(path)
                self.original_emit({'event': 'test', 'count': 12})
                files = list(Path(directory).glob('*.jsonl'))
                self.assertEqual(len(files), 1)
                event = json.loads(files[0].read_text())
                self.assertEqual(event['count'], 12)
                self.assertIn('timestamp', event)
                self.config.BENCHMARK_ENABLED = False
                self.original_emit({'event': 'disabled'})
                self.assertEqual(len(files[0].read_text().splitlines()), 1)
        finally:
            for handler in logger.handlers:
                handler.close()
            logger.handlers[:] = saved_handlers
            benchmark._logger.cache_clear()


if __name__ == '__main__':
    unittest.main()
