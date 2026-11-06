"""Regression coverage for the merge and the supplied public API captures."""
import dataclasses
import json
import os
from pathlib import Path
import re
import sys
from unittest.mock import patch

import httpx
import openai

from test_modes import ModesTest, PAYG_KEY, SEAT_KEY, METER, ROOT

# Reuse fixture setup without collecting a second copy of the inherited suite.
Base = ModesTest
del ModesTest


class MergeTest(Base):
    # Only this class's additional tests are collected by load_tests below.
    def test_captured_payg_catalog_and_output_limits(self):
        self.payg()
        body = json.loads((ROOT / 'tests/fixtures/payg-models.json').read_text())
        self.json_routes({self.plugin.PAYG_BASE + '/models': body})
        ids = self.plugin.quotum.fetch_models()
        self.assertEqual(ids, [row['id'] for row in body['data']])
        self.assertEqual(len(ids), 12)
        for row in body['data']:
            self.assertLessEqual(self.plugin.quotum.get_max_tokens(row['id']), row['max_tokens'])
            self.assertLessEqual(self.plugin.quotum.get_max_tokens(row['id']), row['max_completion_tokens'])
        self.assertEqual(self.plugin.quotum.get_max_tokens(None), 2048)
        self.assertTrue(all(req.get_header('Authorization') is None for req in self.seen))

    def test_captured_nokey_and_badkey_are_nested_401(self):
        self.payg()
        for case in ('nokey', 'badkey'):
            captured = json.loads((ROOT / f'tests/fixtures/payg-chat-{case}.json').read_text())
            self.assertEqual(captured['status'], 401)
            self.assertEqual(self.plugin.payg.error_code(captured['body']), 'invalid-key')
            self.assertEqual(set(captured['body']['error']), {'code', 'message'})
            client = self.sdk_client(lambda req: httpx.Response(captured['status'], json=captured['body']))
            with self.assertRaises(openai.APIStatusError) as caught:
                client.chat.completions.create(model='openai-gpt-oss-120b', messages=[])
            self.assertEqual(caught.exception.status_code, 401)
            message = caught.exception.body['message']
            self.assertIn('missing, invalid, expired or revoked', message)
            self.assertIn('https://quotum.org/payg', message)
            self.assertNotIn('seat', message)
            verdict = self.plugin.classify_error(status_code=401, body=captured['body'])
            self.assertFalse(verdict['should_rotate_credential'])
            self.assertFalse(verdict['should_fallback'])

    def test_payg_402_hints_are_preserved_through_sdk_and_classifier(self):
        self.payg()
        for code, phrase in [('session_limit', 'raise the session limit'), ('insufficient_credit', 'top up your project')]:
            requests = []
            def handler(req):
                requests.append(req)
                return httpx.Response(402, headers={'x-should-retry': 'true'},
                                      json={'error': {'code': code, 'message': SEAT_KEY}})
            client = self.sdk_client(handler)
            with self.assertRaises(openai.APIStatusError) as caught:
                client.chat.completions.create(model='openai-gpt-oss-120b', messages=[])
            body = caught.exception.body
            self.assertEqual(len(requests), 1)
            self.assertIn(phrase, body['message'])
            self.assertIn('https://quotum.org/payg', body['message'])
            self.assertNotIn('bell', body['message'])
            self.assertNotIn(SEAT_KEY, str(caught.exception))
            verdict = self.plugin.classify_error(status_code=402, body=body)
            self.assertIn(phrase, verdict['error_context']['message'])
            for flag in ('retryable', 'should_rotate_credential', 'should_fallback'):
                self.assertFalse(verdict[flag])

    def test_seat_402_keeps_seat_hint_with_blank_payg_key(self):
        os.environ['QUOTUM_PAYG_KEY'] = ' '
        self.json_routes({self.plugin.SITE + '/api/meter/' + self.plugin.key_hash(SEAT_KEY):
                          {'bellAt': '2099-11-06T20:00:00Z'}})
        client = self.sdk_client(lambda req: httpx.Response(402, json={'error': {'code': 'session_limit'}}))
        with self.assertRaises(openai.APIStatusError) as caught:
            client.chat.completions.create(model='kimi-k3', messages=[])
        self.assertEqual(caught.exception.body['type'], 'quotum_cap_spent')
        self.assertIn('cap is spent', str(caught.exception))
        self.assertIn('2099-11-06 20:00 UTC', str(caught.exception))
        self.assertNotIn('payg', str(caught.exception).lower())

    def test_preflight_in_both_modes_never_blocks_chat(self):
        for payg_mode in (False, True):
            with self.subTest(payg=payg_mode):
                os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if payg_mode else ''
                os.environ['QUOTUM_PREFLIGHT'] = '1'
                self.plugin.quotum.build_extra_body(session_id='merge-session', model='chosen')
                calls = []
                def handler(req):
                    calls.append(req)
                    return httpx.Response(200, json={'id': 'continues', 'choices': []})
                with patch.object(self.plugin, 'preflight', side_effect=OSError('metadata failure')) as preflight, patch.object(self.plugin, '_get_json', side_effect=OSError('offline')):
                    reply = self.sdk_client(handler).chat.completions.create(model='chosen', messages=[])
                self.assertEqual(reply.id, 'continues')
                self.assertEqual(json.loads(calls[0].content)['model'], 'chosen')
                self.assertEqual(preflight.call_count, 0 if payg_mode else 1)

    def test_payg_preflight_never_reads_seat_catalog_or_meter(self):
        self.payg()
        os.environ['QUOTUM_PREFLIGHT'] = '1'
        self.plugin.quotum.build_extra_body(session_id='payg-session', model='chosen')
        with patch.object(self.plugin, 'meter') as meter, patch.object(self.plugin, 'catalog') as catalog:
            self.sdk_client(lambda req: httpx.Response(200, json={'id': 'ok', 'choices': []})).chat.completions.create(model='chosen', messages=[])
        meter.assert_not_called()
        catalog.assert_not_called()
        self.assertFalse(self.plugin._WARNED)

    def test_no_mode_leaks_the_other_key_variable_in_messages(self):
        for payg_mode in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if payg_mode else ''
            os.environ['QUOTUM_SEAT_KEY'] = ''
            other = 'QUOTUM_SEAT_KEY' if payg_mode else 'QUOTUM_PAYG_KEY'
            with patch.object(self.plugin, 'meter', return_value=None), patch.object(self.plugin, '_get_json', side_effect=OSError):
                snapshot = self.plugin.quotum.fetch_account_usage()
                self.assertNotIn(other, json.dumps(dataclasses.asdict(snapshot), default=str))
                verdict = self.plugin.classify_error(status_code=402, error_code='session_limit')
                self.assertNotIn(other, json.dumps(verdict))
                if payg_mode:
                    self.assertIn('session limit', verdict['error_context']['message'])
                else:
                    self.assertIn('QUOTUM_SEAT_KEY', snapshot.unavailable_reason)
            for status in (401, 402):
                client = self.sdk_client(lambda req: httpx.Response(status, json={'error': {'message': other}}))
                with patch.object(self.plugin, 'meter', return_value=None), self.assertRaises(openai.APIStatusError) as caught:
                    client.chat.completions.create(model='chosen', messages=[])
                self.assertNotIn(other, str(caught.exception))

    def test_profile_scope_overrides_process_keys_and_does_not_read_files(self):
        scope = sys.modules['agent.secret_scope']
        with patch.object(scope, 'get_secret', side_effect=lambda name: {'QUOTUM_PAYG_KEY': PAYG_KEY}.get(name, '')), patch.object(Path, 'read_text', side_effect=AssertionError('no credential file reads')):
            self.assertEqual(self.plugin.payg_key(), PAYG_KEY)
            self.assertEqual(self.api._env('QUOTUM_PAYG_KEY'), PAYG_KEY)
        with patch.object(scope, 'get_secret', side_effect=RuntimeError('unscoped')):
            with self.assertRaisesRegex(RuntimeError, 'unscoped'):
                self.plugin.payg_key()

    def test_metadata_redirects_refused_in_both_backends(self):
        from types import SimpleNamespace
        from urllib.error import HTTPError
        for handler in (self.plugin._NoRedirect(), self.api._NoRedirect()):
            with self.assertRaises(HTTPError):
                handler.redirect_request(SimpleNamespace(full_url='https://quotum.org'), None, 302, 'redirect', {}, 'https://elsewhere.example')

    def test_payg_catalog_excludes_unavailable_and_nonchat(self):
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + '/models': {'data': [
            {'id': 'live', 'privacy': 'private', 'type': 'text', 'endpoints': ['/chat/completions'], 'available': True},
            {'id': 'down', 'privacy': 'private', 'available': False},
            {'id': 'other', 'privacy': 'private', 'endpoints': ['/embeddings']}]}})
        self.assertEqual(self.plugin.quotum.fetch_models(), ['live'])

    def test_untrusted_error_code_never_echoed_and_scope_hint_survives(self):
        self.payg()
        for code, status, expected in [(PAYG_KEY, 401, 'create a new'), (PAYG_KEY, 402, 'top up'),
                                        (PAYG_KEY, 500, 'request failed'), ('key-model-scope', 403, 'choose a model')]:
            client = self.sdk_client(lambda req: httpx.Response(status, json={'error': {'code': code, 'message': SEAT_KEY}}))
            with self.assertRaises(openai.APIStatusError) as caught:
                client.chat.completions.create(model='chosen', messages=[])
            self.assertNotIn(PAYG_KEY, str(caught.exception))
            self.assertNotIn(SEAT_KEY, str(caught.exception))
            self.assertIn(expected, caught.exception.body['message'])
            if status == 403:
                verdict = self.plugin.classify_error(status_code=status, body=caught.exception.body)
                self.assertIn(expected, verdict['error_context']['message'])

    def test_public_tree_content_grep(self):
        # Byte scanning covers every shipped file, including dashboard assets.
        forbidden = [chr(0x2014).encode(), ('/' + 'Users/').encode()]
        address = re.compile(rb'0x[0-9a-fA-F]{40}')
        ip = re.compile(rb'(?<![\d.])(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?:\.(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}(?![\d.])')
        for path in sorted(ROOT.rglob('*')):
            if not path.is_file():
                continue
            raw = path.read_bytes()
            for token in forbidden:
                self.assertNotIn(token, raw, str(path.relative_to(ROOT)))
            self.assertIsNone(address.search(raw), str(path.relative_to(ROOT)))
            self.assertIsNone(ip.search(raw), str(path.relative_to(ROOT)))


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(MergeTest(name) for name in sorted(MergeTest.__dict__) if name.startswith('test_'))
