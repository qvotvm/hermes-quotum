"""Adversarial regression tests using only synthetic credentials and local transports."""
import importlib.abc
import importlib.machinery
import io
import json
import os
import sys
import types
import unittest
from unittest.mock import patch

import httpx
import openai
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_modes import ModesTest, PAYG_KEY, SEAT_KEY, METER
Base = ModesTest
del ModesTest


class BrokenScope(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, error):
        self.error = error

    def find_spec(self, fullname, path, target=None):
        if fullname == 'agent.secret_scope':
            return importlib.machinery.ModuleSpec(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        raise self.error


class ReviewTests(Base):
    def test_absent_scope_and_absent_accessor_reject_environment(self):
        for scope in (None, types.ModuleType('agent.secret_scope')):
            with self.subTest(scope=scope), patch.dict(sys.modules, {'agent.secret_scope': scope}):
                for helper in (self.plugin.payg, self.api.payg_helpers):
                    for name in ('QUOTUM_SEAT_KEY', 'QUOTUM_PAYG_KEY'):
                        with self.assertRaisesRegex(RuntimeError, 'upgrade Hermes'):
                            helper.secret(name)

    def test_broken_scope_import_never_uses_another_profiles_environment(self):
        package = types.ModuleType('agent')
        package.__path__ = []
        for error in (ImportError('scope failed to initialize'), ModuleNotFoundError('scope dependency absent', name='scope_dependency')):
            for helper in (self.plugin.payg, self.api.payg_helpers):
                with self.subTest(error=type(error).__name__, helper=helper.__name__), patch.dict(sys.modules, {'agent': package}), patch.object(sys, 'meta_path', [BrokenScope(error), *sys.meta_path]):
                    sys.modules.pop('agent.secret_scope', None)
                    with self.assertRaises(type(error)):
                        helper.secret('QUOTUM_SEAT_KEY')

    def test_empty_profile_does_not_fall_back_to_process_key(self):
        with patch('agent.secret_scope.get_secret', return_value=None):
            self.assertEqual(self.plugin.payg.secret('QUOTUM_SEAT_KEY'), '')
            self.assertEqual(self.api._env('QUOTUM_SEAT_KEY'), '')

    def test_seat_nonterminal_errors_do_not_reflect_keys(self):
        for status in (400, 403, 404, 422, 429, 500):
            with self.subTest(status=status):
                client = self.sdk_client(lambda req: httpx.Response(status, json={'error': {'message': SEAT_KEY, 'code': SEAT_KEY}}))
                with client.with_options(max_retries=0) as once, self.assertRaises(openai.APIStatusError) as caught:
                    once.chat.completions.create(model='chosen', messages=[])
                self.assertNotIn(SEAT_KEY, str(caught.exception))
                self.assertNotIn(SEAT_KEY, json.dumps(caught.exception.body))

    def test_error_headers_do_not_leak_keys_to_sdk_debug_logs(self):
        for payg_mode in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if payg_mode else ''
            key = PAYG_KEY if payg_mode else SEAT_KEY
            with self.subTest(payg=payg_mode):
                client = self.sdk_client(lambda req: httpx.Response(401, headers={'x-request-id': key, 'x-diagnostic': key}, json={'error': {'message': key}}))
                with self.assertLogs('openai', level='DEBUG') as logs, self.assertRaises(openai.APIStatusError) as caught:
                    client.chat.completions.create(model='chosen', messages=[])
                self.assertNotIn(key, str(caught.exception))
                self.assertNotIn(key, '\n'.join(logs.output))

    def test_stream_errors_do_not_reflect_keys(self):
        for payg_mode in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if payg_mode else ''
            key = PAYG_KEY if payg_mode else SEAT_KEY
            for event in ('', 'event: error\r\n'):
                with self.subTest(payg=payg_mode, event=event):
                    payload = json.dumps({'error': {'message': key, 'code': key}})
                    wire = (event + 'data: ' + payload + '\r\n\r\n').encode()
                    class Chunks(httpx.SyncByteStream):
                        def __iter__(self):
                            for n in range(0, len(wire), 3):
                                yield wire[n:n + 3]
                    client = self.sdk_client(lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Chunks()))
                    with self.assertRaises(openai.APIError) as caught:
                        with client.chat.completions.create(model='chosen', messages=[], stream=True) as stream:
                            list(stream)
                    self.assertNotIn(key, str(caught.exception))
                    self.assertNotIn(key, json.dumps(caught.exception.body))

    def test_x_search_failure_logs_no_exception_text(self):
        with patch.object(self.plugin, '_get_json', side_effect=OSError(SEAT_KEY)), self.assertLogs(self.plugin.logger, level='DEBUG') as logs:
            self.assertEqual(self.plugin.x_search_models(), set())
        self.assertNotIn(SEAT_KEY, '\n'.join(logs.output))

    def test_dashboard_http_and_json_errors_are_safe_strings(self):
        for payg_mode in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if payg_mode else ''
            key = PAYG_KEY if payg_mode else SEAT_KEY
            url = self.plugin.PAYG_BASE + '/meter' if payg_mode else self.plugin.SITE + '/api/meter/' + self.plugin.key_hash(key)
            for status in (200, 401, 402, 500):
                with self.subTest(payg=payg_mode, status=status):
                    body = {'error': {'message': key, 'code': key}, 'debug': key}
                    if status != 200:
                        body = self.plugin.urllib.error.HTTPError(url, status, 'fixture', {}, io.BytesIO(json.dumps(body).encode()))
                    self.json_routes({url: body})
                    result = self.api.account()
                    self.assertIsInstance(result['error'], str)
                    self.assertNotIn(key, json.dumps(result))

    def test_status_error_does_not_reach_usage_or_dashboard(self):
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + '/meter': METER, self.plugin.PAYG_BASE + '/status': {'error': PAYG_KEY}})
        self.assertNotIn(PAYG_KEY, json.dumps(self.api.account()))
        self.assertNotIn(PAYG_KEY, str(self.plugin.quotum.fetch_account_usage()))

    def test_dashboard_without_credentials_is_a_local_setup_state(self):
        os.environ.clear()
        app = FastAPI()
        app.include_router(self.api.router, prefix='/api/plugins/quotum')
        with patch.object(self.api, '_get', side_effect=AssertionError('no network without a key')) as get, TestClient(app) as client:
            for route in ('account', 'record', 'payg'):
                response = client.get('/api/plugins/quotum/' + route)
                self.assertEqual(response.status_code, 200)
                self.assertIsInstance(response.json()['error'], str)
                self.assertIn('QUOTUM_', response.json()['error'])
            get.assert_not_called()

    def test_stream_preserves_unicode_multiline_receipt_and_closes(self):
        for payg_mode in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if payg_mode else ''
            frame = {'id': 'reply', 'choices': [{'index': 0, 'delta': {'content': 'caf\u00e9'}}],
                     'quotum_billing': METER['receipts'][0]}
            wire = (': keepalive\r\n\r\n' +
                    '\r\n'.join('data: ' + line for line in json.dumps(frame, indent=2, ensure_ascii=False).splitlines()) +
                    '\r\n\r\ndata: [DONE]\r\n\r\n').encode()
            closed = []
            class Chunks(httpx.SyncByteStream):
                def __iter__(self):
                    for n in range(0, len(wire), 3):
                        yield wire[n:n + 3]
                def close(self):
                    closed.append(True)
            client = self.sdk_client(lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Chunks()))
            with client.chat.completions.create(model='chosen', messages=[], stream=True) as stream:
                replies = list(stream)
            self.assertEqual(replies[0].choices[0].delta.content, 'caf\u00e9')
            self.assertEqual(replies[0].quotum_billing, METER['receipts'][0])
            self.assertEqual(closed, [True])

    def test_transient_seat_retry_controls_survive_error_sanitizing(self):
        calls = []
        def handler(req):
            calls.append(req)
            return httpx.Response(429, headers={'retry-after': '2', 'x-should-retry': 'true', 'x-diagnostic': SEAT_KEY},
                                  json={'error': {'message': SEAT_KEY}})
        client = self.sdk_client(handler)
        with patch.object(openai.OpenAI, '_sleep_for_retry') as sleep, client.with_options(max_retries=1) as once:
            with self.assertRaises(openai.APIStatusError) as caught:
                once.chat.completions.create(model='chosen', messages=[])
        self.assertEqual(len(calls), 2)
        sleep.assert_called_once()
        self.assertEqual(caught.exception.response.headers['retry-after'], '2.0')
        self.assertNotIn(SEAT_KEY, str(caught.exception.response.headers))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ReviewTests(name) for name in sorted(ReviewTests.__dict__) if name.startswith('test_'))
