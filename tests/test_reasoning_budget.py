"""Output-budget failures and SDK retry controls, with offline HTTP fixtures."""
import copy
import errno
import json
import os
import unittest
from unittest.mock import patch

import httpx
import openai

from test_modes import ModesTest, PAYG_KEY, ROOT, SEAT_KEY
Base = ModesTest
del ModesTest


class ReasoningBudgetTests(Base):
    def setUp(self):
        super().setUp()
        self.fixtures = json.loads((ROOT / 'tests/fixtures/reasoning-budget.json').read_text())

    def modes(self):
        for enabled in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if enabled else ''
            yield enabled

    def client_for(self, body, *, streaming=False, headers=None):
        self.calls = []
        self.closed = []
        wire = (''.join('data: ' + json.dumps(frame) + '\r\n\r\n' for frame in body) + 'data: [DONE]\r\n\r\n'
                if streaming else json.dumps(body)).encode()
        owner = self
        class Chunks(httpx.SyncByteStream):
            def __iter__(self):
                for n in range(0, len(wire), 3):
                    yield wire[n:n + 3]
            def close(self):
                owner.closed.append(True)
        def handler(req):
            self.calls.append(req)
            return httpx.Response(200, headers=headers if headers is not None else {
                'content-type': 'text/event-stream' if streaming else 'application/json'}, stream=Chunks())
        return self.sdk_client(handler)

    def assert_terminal(self, error):
        self.assertEqual(error.code, self.plugin.OUTPUT_BUDGET_CODE)
        self.assertIn('reasoning used the whole output budget', str(error))
        self.assertIn('pick a non-reasoning model', str(error))
        verdict = self.plugin.classify_error(error, status_code=getattr(error, 'status_code', None), body=error.body)
        self.assertEqual(verdict['reason'], 'format_error')
        self.assertTrue(all(verdict[name] is False for name in (
            'retryable', 'should_compress', 'should_rotate_credential', 'should_fallback')))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.closed, [True])

    def test_nonstream_empty_and_null_length_are_terminal(self):
        for enabled in self.modes():
            for content in ('', None):
                for limits in ({'max_tokens': 64}, {'max_completion_tokens': 96}, {}):
                    with self.subTest(payg=enabled, content=content, limits=limits):
                        body = copy.deepcopy(self.fixtures['empty'])
                        body['choices'][0]['message']['content'] = content
                        client = self.client_for(body)
                        with patch.object(openai.OpenAI, '_sleep_for_retry') as sleep, self.assertRaises(openai.BadRequestError) as caught:
                            client.chat.completions.create(model='chosen', messages=[], **limits)
                        self.assert_terminal(caught.exception)
                        self.assertEqual(caught.exception.response.headers['x-should-retry'], 'false')
                        sleep.assert_not_called()
                        for name, value in limits.items():
                            self.assertIn(f'{name} {value}', str(caught.exception))
                        if not limits:
                            self.assertNotIn('budget (', str(caught.exception))

    def test_reasoning_only_stream_is_terminal(self):
        for enabled in self.modes():
            for limits in ({'max_tokens': 64}, {'max_completion_tokens': 96}, {}):
                for headers in (None, {}):
                    with self.subTest(payg=enabled, limits=limits, headers=headers):
                        client = self.client_for(self.fixtures['reasoning_stream'], streaming=True, headers=headers)
                        with patch.object(openai.OpenAI, '_sleep_for_retry') as sleep, self.assertRaises(openai.APIError) as caught:
                            with client.chat.completions.create(model='chosen', messages=[], stream=True, **limits) as stream:
                                list(stream)
                        self.assert_terminal(caught.exception)
                        sleep.assert_not_called()
                        for name, value in limits.items():
                            self.assertIn(f'{name} {value}', str(caught.exception))

    def test_json_reply_to_stream_request_is_terminal(self):
        for enabled in self.modes():
            with self.subTest(payg=enabled):
                client = self.client_for(self.fixtures['empty'])
                with self.assertRaises(openai.APIError) as caught:
                    with client.chat.completions.create(model='chosen', messages=[], stream=True, max_tokens=64) as stream:
                        list(stream)
                self.assert_terminal(caught.exception)

    def test_normal_and_truncated_nonstream_responses_unchanged(self):
        for enabled in self.modes():
            for name in ('normal', 'truncated'):
                with self.subTest(payg=enabled, fixture=name):
                    body = self.fixtures[name]
                    client = self.client_for(body)
                    reply = client.chat.completions.with_raw_response.create(model='chosen', messages=[], max_tokens=64)
                    self.assertEqual(json.loads(reply.http_response.content), body)
                    self.assertEqual(len(self.calls), 1)
                    self.assertEqual(self.closed, [True])

    def test_normal_and_truncated_stream_responses_unchanged(self):
        for enabled in self.modes():
            for reason in ('stop', 'length'):
                with self.subTest(payg=enabled, reason=reason):
                    body = copy.deepcopy(self.fixtures['answer_stream'])
                    body[2]['choices'][0]['finish_reason'] = reason
                    client = self.client_for(body, streaming=True)
                    with client.chat.completions.with_streaming_response.create(model='chosen', messages=[], stream=True) as reply:
                        frames = [json.loads(line[6:]) for line in reply.iter_lines() if line.startswith('data: ') and line != 'data: [DONE]']
                    self.assertEqual(frames, body)
                    self.assertEqual(self.closed, [True])

    def test_tool_calls_and_other_answer_output_with_length_unchanged(self):
        for enabled in self.modes():
            for answer in ({'tool_calls': [{'id': 'call-fixture', 'type': 'function', 'function': {'name': 'read', 'arguments': '{}'}}]},
                           {'function_call': {'name': 'read', 'arguments': '{}'}}, {'refusal': 'cannot answer'},
                           {'audio': {'id': 'audio-fixture', 'transcript': 'hello'}}, {'content': ' '}):
                for streaming in (False, True):
                    with self.subTest(payg=enabled, answer=answer, streaming=streaming):
                        body = copy.deepcopy(self.fixtures['empty'])
                        body['choices'][0]['message'].update(answer)
                        if streaming:
                            choice = body['choices'][0]
                            choice['delta'] = choice.pop('message')
                        client = self.client_for([body] if streaming else body, streaming=streaming)
                        reply = client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                        if streaming:
                            with reply:
                                self.assertEqual(len(list(reply)), 1)
                        else:
                            self.assertEqual(reply.choices[0].finish_reason, 'length')

    def test_state_is_per_choice_and_per_request(self):
        for enabled in self.modes():
            with self.subTest(payg=enabled):
                frames = copy.deepcopy(self.fixtures['reasoning_stream'])
                frames.insert(1, {'choices': [{'index': 1, 'delta': {'content': 'other choice'}}]})
                client = self.client_for(frames, streaming=True)
                for attempt in range(2):
                    with self.assertRaises(openai.APIError):
                        with client.chat.completions.create(model='chosen', messages=[], stream=True) as stream:
                            list(stream)
                self.assertEqual(len(self.calls), 2)

    def test_stop_with_empty_content_is_outside_budget_guard(self):
        for enabled in self.modes():
            body = copy.deepcopy(self.fixtures['empty'])
            body['choices'][0]['finish_reason'] = 'stop'
            reply = self.client_for(body).chat.completions.create(model='chosen', messages=[])
            self.assertEqual(reply.choices[0].message.content, '')

    def test_output_limits_pass_through_without_transport_default_or_cap(self):
        for enabled in self.modes():
            for limits in ({}, {'max_tokens': 64}, {'max_completion_tokens': 96}, {'max_tokens': 8192}):
                with self.subTest(payg=enabled, limits=limits):
                    self.client_for(self.fixtures['normal']).chat.completions.create(model='chosen', messages=[], **limits)
                    wire = json.loads(self.calls[0].content)
                    self.assertEqual({k: wire[k] for k in ('max_tokens', 'max_completion_tokens') if k in wire}, limits)
            self.assertEqual(self.plugin.quotum.get_max_tokens('chosen'), 2048 if enabled else None)

    def test_504_429_and_connect_failures_default_to_one_attempt(self):
        for enabled in self.modes():
            for failure in (504, 429, httpx.ReadTimeout('fixture'), OSError(errno.ETIMEDOUT, 'fixture')):
                for kwargs in ({}, {'max_retries': 0}, {'max_retries': 1}):
                    with self.subTest(payg=enabled, failure=failure, kwargs=kwargs):
                        calls = []
                        def handler(req):
                            calls.append(req)
                            if isinstance(failure, Exception):
                                raise failure
                            return httpx.Response(failure, json={'error': {'message': 'fixture'}})
                        with patch.object(httpx.HTTPTransport, 'handle_request', side_effect=httpx.MockTransport(handler).handle_request), \
                                self.plugin.quotum.create_client(api_key=SEAT_KEY, base_url=self.plugin.VENICE_BASE, **kwargs) as client:
                            retries = 0 if enabled else kwargs.get('max_retries', 0)
                            self.assertEqual(client.max_retries, retries)
                            with patch.object(openai.OpenAI, '_sleep_for_retry') as sleep, self.assertRaises(openai.APIError):
                                client.chat.completions.create(model='chosen', messages=[])
                            self.assertEqual(len(calls), 1 + retries)
                            self.assertEqual(sleep.call_count, retries)
                            if not enabled:
                                self.assertTrue(all('idempotency-key' not in req.headers for req in calls))

    def test_body_read_retries_depend_on_streaming(self):
        for enabled in self.modes():
            for streaming in (False, True):
                calls = []
                class Broken(httpx.SyncByteStream):
                    def __iter__(self):
                        raise httpx.ReadTimeout('fixture')
                        yield b''
                def handler(req):
                    calls.append(req)
                    return httpx.Response(200, headers={'content-type': 'text/event-stream' if streaming else 'application/json'}, stream=Broken())
                client = self.sdk_client(handler)
                if not enabled:
                    client = client.with_options(max_retries=1)
                with patch.object(openai.OpenAI, '_sleep_for_retry'), self.assertRaises((openai.APIConnectionError, httpx.ReadError)):
                    result = client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                    if streaming:
                        with result:
                            list(result)
                self.assertEqual(len(calls), 2 if not enabled and not streaming else 1)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ReasoningBudgetTests(name) for name in sorted(ReasoningBudgetTests.__dict__) if name.startswith('test_'))
