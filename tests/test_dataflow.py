"""Data-flow boundaries, exercised with synthetic credentials and offline peers."""
import copy
import dataclasses
import io
import json
import os
import traceback
import unittest
import urllib.error
from unittest.mock import patch

import httpx
import openai

from test_modes import ModesTest, METER, SEAT_KEY, PAYG_KEY
Base = ModesTest
del ModesTest


class DataflowTests(Base):
    def modes(self):
        for enabled in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if enabled else ''
            yield PAYG_KEY if enabled else SEAT_KEY

    def test_chat_success_allowlist_and_both_credentials(self):
        for key in self.modes():
            for streaming in (False, True):
                with self.subTest(key=key, streaming=streaming):
                    body = {'id': 'reply', 'choices': [{'index': 0,
                        'delta' if streaming else 'message': {'role': 'assistant', 'content': 'hello ' + key,
                            'diagnostic': {'private': 'upstream-only'}}, 'diagnostic': 'upstream-only'}],
                        'diagnostic': 'upstream-only', 'quotum_billing': copy.deepcopy(METER['receipts'][0])}
                    body['quotum_billing']['diagnostic'] = 'upstream-only'
                    body['quotum_billing']['model'] = SEAT_KEY
                    wire = ('data: ' + json.dumps(body) + '\n\ndata: [DONE]\n\n') if streaming else json.dumps(body)
                    client = self.sdk_client(lambda req: httpx.Response(200, content=wire,
                        headers={'content-type': 'text/event-stream' if streaming else 'application/json'}))
                    reply = client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                    if streaming:
                        with reply:
                            exposed = ''.join(part.model_dump_json() for part in reply)
                    else:
                        exposed = reply.model_dump_json()
                    self.assertNotIn(key, exposed)
                    self.assertNotIn(SEAT_KEY, exposed)
                    self.assertNotIn('upstream-only', exposed)
                    self.assertIn('hello', exposed)
                    self.assertIn('37500', exposed)

    def test_malformed_json_and_sse_fail_with_fixed_error(self):
        for key in self.modes():
            for streaming in (False, True):
                for body in (key, json.dumps({'choices': key}), json.dumps([key])):
                    with self.subTest(key=key, streaming=streaming, body=body):
                        wire = 'data: ' + body + '\n\n' if streaming else body
                        client = self.sdk_client(lambda req: httpx.Response(200, content=wire,
                            headers={'content-type': 'text/event-stream' if streaming else 'application/json'}))
                        with self.assertLogs('openai', level='DEBUG') as logs:
                            try:
                                reply = client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                                if streaming:
                                    with reply:
                                        replies = list(reply)
                                    exposed = str(replies)
                                else:
                                    exposed = str(reply)
                            except Exception as exc:
                                exposed = ''.join(traceback.format_exception(exc)) + str(getattr(exc, 'doc', '')) + str(getattr(exc, 'body', ''))
                        self.assertNotIn(key, exposed + '\n'.join(logs.output))

    def test_sse_only_data_and_known_chat_fields_reach_sdk(self):
        for key in self.modes():
            wire = f': {key}\nevent: thread.{key}\nid: {key}\nretry: 7\ndata: ' + json.dumps({'id': 'ok', 'choices': [], 'diagnostic': key}) + '\n\ndata: [DONE]\n\n'
            client = self.sdk_client(lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire))
            with client.chat.completions.with_streaming_response.create(model='chosen', messages=[], stream=True) as reply:
                exposed = b''.join(reply.iter_bytes()).decode()
            self.assertNotIn(key, exposed)
            self.assertNotIn('event:', exposed)
            self.assertNotIn('retry:', exposed)

    def test_read_and_close_exceptions_are_safe_including_decoder_errors(self):
        for key in self.modes():
            for failure in (httpx.DecodingError, OSError):
                for status in (200, 401, 402):
                    with self.subTest(key=key, failure=failure, status=status):
                        class Broken(httpx.SyncByteStream):
                            def __iter__(self):
                                raise failure(key)
                                yield b''
                            def close(self):
                                raise OSError(key)
                        client = self.sdk_client(lambda req: httpx.Response(status, headers={'content-type': 'application/json'}, stream=Broken())).with_options(max_retries=0)
                        with self.assertLogs('openai', level='DEBUG') as logs, self.assertRaises(Exception) as caught:
                            client.chat.completions.create(model='chosen', messages=[])
                        self.assertNotIn(key, ''.join(traceback.format_exception(caught.exception)) + '\n'.join(logs.output))
                        if status != 200:
                            self.assertEqual(caught.exception.status_code, status)

    def test_metadata_error_close_failure_keeps_safe_status(self):
        self.payg()
        class Broken(io.BytesIO):
            def close(self):
                super().close()
                raise OSError(PAYG_KEY)
        for route in (self.api.account, self.plugin.quotum.fetch_account_usage):
            with self.subTest(route=route.__name__):
                error = urllib.error.HTTPError(self.plugin.PAYG_BASE + '/meter', 402, 'fixture', {}, Broken(b'{}'))
                self.json_routes({self.plugin.PAYG_BASE + '/meter': error})
                result = route()
                self.assertIn('top up', str(result))
                self.assertNotIn(PAYG_KEY, str(result))

    def test_seat_meter_has_typed_allowlist_and_rejects_error_balance(self):
        base = {'seat': 3, 'session': 5, 'kind': 'seat', 'capUsd': 2, 'spentUsd': 1, 'leftUsd': 1,
                'lastBell': {'session': 4, 'burnedUsd': .5, 'diagnostic': 'upstream-only'},
                'readAt': '2026-11-06T01:02:03Z', 'diagnostic': 'upstream-only'}
        self.json_routes({self.plugin.SITE + '/api/meter/' + self.plugin.key_hash(SEAT_KEY): base})
        result = self.plugin.quotum.fetch_account_usage()
        self.assertNotIn('upstream-only', str(result))
        self.assertEqual(result.windows[0].used_percent, 50)
        for change in ({'error': 'unknown'}, {'lastBell': SEAT_KEY}, {'session': {'data': SEAT_KEY}},
                       {'capUsd': float('inf')}, {'spentUsd': True}, {'capUsd': 10 ** 500}):
            with self.subTest(change=str(change)[:60]):
                self.plugin._METER_CACHE.clear()
                self.json_routes({self.plugin.SITE + '/api/meter/' + self.plugin.key_hash(SEAT_KEY): {**base, **change}})
                result = self.plugin.quotum.fetch_account_usage()
                self.assertNotIn(SEAT_KEY, str(result))
                json.dumps(dataclasses.asdict(result), default=str, allow_nan=False)
                if 'error' in change:
                    self.assertIsNotNone(result.unavailable_reason)

    def test_seat_record_allowlist_including_url_and_image_fields(self):
        record = {'record': {'seals': {'used': 1, 'other': 'upstream-only'}, 'counts': {'used': 100}, 'diagnostic': 'upstream-only'},
                  'wallet': 'https://elsewhere.example/upstream-only', 'rootTx': '../upstream-only',
                  'diagnostic': 'upstream-only', 'catalog': [
                      {'id': 'used', 'name': 'Used', 'family': 'seat', 'tiers': [10, 100, 1000], 'diagnostic': 'upstream-only'},
                      {'id': '../upstream-only', 'name': 'bad', 'family': 'seat', 'tiers': [1]}]}
        self.json_routes({self.plugin.SITE + '/api/meter/' + self.plugin.key_hash(SEAT_KEY): {'seat': 3},
                          self.plugin.SITE + '/api/record?seat=3': record})
        for route in (self.api.account, self.api.record):
            result = route()
            self.assertNotIn('upstream-only', json.dumps(result))
            self.assertEqual(result['record']['seals']['used'], 1)
            self.assertEqual(result['catalog'][0]['name'], 'Used')
        self.api._CACHE.clear()
        record.update(wallet='0x' + 'a' * 40, rootTx='0x' + 'b' * 64, session=5)
        result = self.api.account()
        self.assertEqual(result['wallet'], record['wallet'])
        self.assertEqual(result['rootTx'], record['rootTx'])
        self.assertEqual(result['session'], 5)
        self.assertEqual(result['record']['counts']['used'], 100)

    def test_payg_presentation_rejects_other_credential_and_nonfinite_values(self):
        self.payg()
        meter = copy.deepcopy(METER)
        meter.update(project=SEAT_KEY, frozen={'diagnostic': SEAT_KEY}, held=10 ** 500)
        meter['receipts'][0].update(status='\x1b[31m' + SEAT_KEY)
        self.json_routes({self.plugin.PAYG_BASE + '/meter': meter, self.plugin.PAYG_BASE + '/status': {'intake': True}})
        for route in (self.api.account, self.plugin.quotum.fetch_account_usage):
            result = route()
            if dataclasses.is_dataclass(result):
                result = dataclasses.asdict(result)
            text = json.dumps(result, default=str, allow_nan=False)
            self.assertNotIn(SEAT_KEY, text)
            self.assertNotIn('\\u001b', text)

    def test_catalog_ids_cannot_be_credentials_or_log_injection(self):
        for key in self.modes():
            self.plugin._CATALOG_CACHE = None
            rows = [{'id': model, 'privacy': 'private', 'in': 1, 'out': 1} for model in
                    ('chosen', key, SEAT_KEY, 'line\nforged', 'escape\x1b[31m', {'diagnostic': key})]
            url = self.plugin.PAYG_BASE + '/models' if key == PAYG_KEY else self.plugin.SITE + '/api/seat/models'
            self.json_routes({url: {'data' if key == PAYG_KEY else 'models': rows}})
            self.assertEqual(self.plugin.quotum.fetch_models(api_key=key), ['chosen'])
            self.plugin._X_MODELS = None
            self.json_routes({self.plugin.VENICE_BASE + '/models?type=text': {'data': [
                {**row, 'model_spec': {'capabilities': {'supportsXSearch': True}}} for row in rows]}})
            self.assertEqual(self.plugin.x_search_models(), {'chosen'})

    def test_preflight_logs_only_valid_model_labels(self):
        os.environ['QUOTUM_PREFLIGHT'] = '1'
        rows = [{'id': 'chosen', 'in': 10, 'out': 10, 'privacy': 'private'},
                {'id': 'cheap\nforged\x1b[31m', 'in': 1, 'out': 1, 'privacy': 'private'}]
        with patch.object(self.plugin, 'catalog', return_value=rows), patch.object(self.plugin, 'meter', return_value={'leftUsd': 0}), patch.object(self.plugin.logger, 'warning') as warn:
            self.plugin.preflight(SEAT_KEY, 'chosen', 'fixture-session')
        for args in warn.call_args_list:
            self.assertNotIn('\n', str(args.args[1:]))
            self.assertNotIn('forged', str(args.args))

    def test_stream_credentials_split_across_deltas_and_normal_prefixes(self):
        for key in self.modes():
            frames = []
            for index, fragment in enumerate(('hello ', key[:7], key[7:], '!', ' fixture-')):
                frames.append({'id': 'stream', 'choices': [{'index': 0, 'delta': {'content': fragment,
                    'tool_calls': [{'index': 0, 'function': {'arguments': fragment}}]}}]})
            frames.append({'id': 'stream', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
            frames.append({'id': 'stream', 'choices': [], 'quotum_billing': METER['receipts'][0]})
            wire = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames) + 'data: [DONE]\n\n'
            client = self.sdk_client(lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=wire))
            with client.chat.completions.create(model='chosen', messages=[], stream=True) as stream:
                replies = list(stream)
            text, args = '', ''
            for reply in replies:
                for choice in reply.choices:
                    text += choice.delta.content or ''
                    for tool in choice.delta.tool_calls or []:
                        args += tool.function.arguments or ''
            self.assertEqual(text, 'hello [redacted]! fixture-')
            self.assertEqual(args, text)
            self.assertEqual(replies[-1].quotum_billing, METER['receipts'][0])

    def test_response_scrubbing_uses_actual_request_key_after_scope_change(self):
        for key in self.modes():
            actual = 'fixture-pooled-key'
            client = self.sdk_client(lambda req: httpx.Response(200, json={'id': 'reply', 'choices': [
                {'index': 0, 'message': {'role': 'assistant', 'content': actual}}]})).with_options(api_key=actual)
            result = client.chat.completions.create(model='chosen', messages=[])
            self.assertNotIn(actual, result.model_dump_json())

    def test_fixed_metadata_hosts_and_bearer_locations(self):
        for key in self.modes():
            seen = []
            def handler(req):
                seen.append(req)
                return httpx.Response(200, json={'id': 'ok', 'choices': []})
            client = self.sdk_client(handler)
            client.chat.completions.create(model='chosen', messages=[{'role': 'user', 'content': 'hello'}])
            req = seen[0]
            self.assertEqual(req.url.host, 'quotum.org' if key == PAYG_KEY else 'api.venice.ai')
            self.assertEqual(req.headers['authorization'], 'Bearer ' + key)
            self.assertNotIn(key, str(req.url) + req.content.decode())
            self.assertTrue(all(key not in v for k, v in req.headers.items() if k != 'authorization'))
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + '/meter': METER, self.plugin.PAYG_BASE + '/status': {'intake': True}})
        self.plugin.quotum.fetch_account_usage()
        self.api.account()
        for req in self.seen:
            self.assertTrue(req.full_url.startswith(self.plugin.PAYG_BASE + '/'))
            self.assertNotIn(PAYG_KEY, req.full_url)
            self.assertEqual(req.get_header('Authorization'), 'Bearer ' + PAYG_KEY if req.full_url.endswith('/meter') else None)

    def test_chat_tools_and_usage_preserved(self):
        for key in self.modes():
            body = {'id': 'tool-reply', 'object': 'chat.completion', 'created': 7, 'model': 'chosen',
                    'choices': [{'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
                        'reasoning_content': 'considering', 'tool_calls': [{'id': 'call-fixture', 'type': 'function',
                            'function': {'name': 'lookup', 'arguments': '{"city":"Paris"}'}}]}}],
                    'usage': {'prompt_tokens': 8, 'completion_tokens': 3, 'total_tokens': 11},
                    'quotum_billing': copy.deepcopy(METER['receipts'][0])}
            client = self.sdk_client(lambda req: httpx.Response(200, json=body))
            result = client.chat.completions.create(model='chosen', messages=[])
            self.assertEqual(result.choices[0].message.tool_calls[0].function.arguments, '{"city":"Paris"}')
            self.assertEqual(result.usage.total_tokens, 11)
            self.assertEqual(result.quotum_billing, METER['receipts'][0])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(DataflowTests(name) for name in sorted(DataflowTests.__dict__) if name.startswith('test_'))
