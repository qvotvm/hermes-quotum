"""Public boundary regressions with synthetic keys and local HTTP transports."""
import copy
import dataclasses
import gzip
import io
import json
import logging
import os
import traceback
import unittest
import urllib.error
from unittest.mock import patch

import httpx
import openai

HTTP_HANDLE = httpx.HTTPTransport.handle_request

from test_modes import ModesTest, METER, PAYG_KEY, SEAT_KEY
Base = ModesTest
del ModesTest


class BoundaryTests(Base):
    def modes(self):
        for enabled in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if enabled else ''
            yield PAYG_KEY if enabled else SEAT_KEY

    def test_redirect_errors_are_sanitized_without_following(self):
        for key in self.modes():
            for status in (301, 302, 307, 308):
                with self.subTest(mode=key, status=status):
                    calls = []
                    def handler(req):
                        calls.append(req)
                        return httpx.Response(status, headers={'location': 'https://elsewhere.example/' + key,
                                              'x-request-id': key}, text=key)
                    client = self.sdk_client(handler)
                    with self.assertLogs('openai', level='DEBUG') as logs, self.assertRaises(openai.APIStatusError) as caught:
                        client.chat.completions.create(model='chosen', messages=[])
                    exposed = str(caught.exception) + str(caught.exception.response.headers) + '\n'.join(logs.output)
                    self.assertNotIn(key, exposed)
                    self.assertEqual(len(calls), 1)

    def test_success_and_sse_headers_never_reflect_credentials(self):
        for key in self.modes():
            for streaming in (False, True):
                with self.subTest(mode=key, streaming=streaming):
                    def handler(req):
                        headers = {'x-request-id': key, 'x-diagnostic': key, 'set-cookie': 'debug=' + key}
                        if streaming:
                            headers['content-type'] = 'text/event-stream'
                            content = 'data: ' + json.dumps({'error': {'message': key}}) + '\n\n'
                        else:
                            headers['content-type'] = 'application/json'
                            content = json.dumps({'id': 'ok', 'choices': []})
                        return httpx.Response(200, headers=headers, content=content,
                                              extensions={'reason_phrase': key.encode()})
                    client = self.sdk_client(handler)
                    with self.assertLogs('openai', level='DEBUG') as logs:
                        if streaming:
                            stream = client.chat.completions.create(model='chosen', messages=[], stream=True)
                            response = stream.response
                            with stream, self.assertRaises(openai.APIError):
                                list(stream)
                        else:
                            raw = client.chat.completions.with_raw_response.create(model='chosen', messages=[])
                            response = raw.http_response
                            self.assertEqual(raw.parse().id, 'ok')
                    self.assertNotIn(key, str(response.headers) + response.reason_phrase + '\n'.join(logs.output))
                    self.assertFalse(client._client.cookies)

    def test_transport_and_stream_failures_do_not_log_exception_text(self):
        for key in self.modes():
            for streaming in (False, True):
                with self.subTest(mode=key, streaming=streaming):
                    closed = []
                    class Broken(httpx.SyncByteStream):
                        def __iter__(self):
                            raise httpx.ReadError(key)
                            yield b''
                        def close(self):
                            closed.append(True)
                    def handler(req):
                        if not streaming:
                            raise httpx.RemoteProtocolError(key)
                        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=Broken())
                    client = self.sdk_client(handler).with_options(max_retries=0)
                    with client, self.assertLogs('openai', level='DEBUG') as logs, self.assertRaises(Exception) as caught:
                        result = client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                        if streaming:
                            with result:
                                list(result)
                    self.assertNotIn(key, ''.join(traceback.format_exception(caught.exception)) + '\n'.join(logs.output))
                    if streaming:
                        self.assertEqual(closed, [True])

    def test_missing_sse_content_type_cannot_bypass_error_filter(self):
        for key in self.modes():
            for headers in ({}, {'content-type': 'text/plain'}):
                with self.subTest(mode=key, headers=headers):
                    wire = 'data: ' + json.dumps({'error': {'message': key}}) + '\n\n'
                    client = self.sdk_client(lambda req: httpx.Response(200, headers=headers, content=wire))
                    with self.assertRaises(openai.APIError) as caught:
                        with client.chat.completions.create(model='chosen', messages=[], stream=True) as stream:
                            list(stream)
                    self.assertNotIn(key, str(caught.exception) + json.dumps(caught.exception.body))

    def test_json_success_error_envelopes_do_not_reflect_credentials(self):
        for key in self.modes():
            for media in ('application/json', None):
                with self.subTest(mode=key, media=media):
                    client = self.sdk_client(lambda req: httpx.Response(200, headers={'content-type': media} if media else {},
                        content=json.dumps({'error': {'message': key}, 'debug': key})))
                    reply = client.chat.completions.create(model='chosen', messages=[])
                    self.assertNotIn(key, reply.model_dump_json())

    def test_wire_trace_cannot_log_headers_before_response_sanitizing(self):
        import httpcore
        from httpcore._trace import Trace
        log = logging.getLogger('httpcore.http11')
        for key in self.modes():
            with self.subTest(mode=key):
                def handler(req):
                    core_request = httpcore.Request('POST', str(req.url), extensions=req.extensions)
                    with Trace('receive_response_headers', log, core_request) as trace:
                        trace.return_value = (b'HTTP/1.1', 200, key.encode(), [(b'x-request-id', key.encode())])
                    return httpx.Response(200, json={'id': 'ok', 'choices': []})
                client = self.sdk_client(handler)
                with self.assertLogs('httpcore', level='DEBUG') as logs:
                    reply = client.chat.completions.create(model='chosen', messages=[])
                    with Trace('unrelated', log) as trace:
                        trace.return_value = 'unrelated diagnostic'
                self.assertEqual(reply.id, 'ok')
                self.assertNotIn(key, '\n'.join(logs.output))
                self.assertIn('unrelated diagnostic', '\n'.join(logs.output))

    def test_real_http_transport_with_memory_peer_preserves_io_and_hides_headers(self):
        from httpcore._backends.mock import MockBackend
        for key in self.modes():
            for status in (200, 401, 402):
                for streaming in (False, True):
                    with self.subTest(mode=key, status=status, streaming=streaming):
                        content = json.dumps({'id': 'wire-ok', 'choices': []} if status == 200 else {'error': {'message': key}})
                        if streaming and status == 200:
                            content = 'data: ' + content + '\n\ndata: [DONE]\n\n'
                        body = content.encode()
                        media = 'text/event-stream' if streaming and status == 200 else 'application/json'
                        wire = (f'HTTP/1.1 {status} OK\r\nContent-Type: {media}\r\nX-Request-ID: {key}\r\n'
                                f'Content-Length: {len(body)}\r\n\r\n').encode() + body
                        client = self.plugin.quotum.create_client(api_key=SEAT_KEY, base_url=self.plugin.VENICE_BASE, max_retries=0)
                        transport = client._client._transport
                        pool = transport._pool if key == SEAT_KEY else transport.transport._pool
                        pool._network_backend = MockBackend([wire])
                        with client, patch.object(httpx.HTTPTransport, 'handle_request', HTTP_HANDLE), patch.object(self.plugin, 'meter', return_value=None), self.assertLogs('httpcore', level='DEBUG') as logs:
                            if status != 200:
                                with self.assertRaises(openai.APIStatusError) as caught:
                                    client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                                self.assertEqual(caught.exception.status_code, status)
                            else:
                                reply = client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                                if streaming:
                                    with reply:
                                        reply = list(reply)[0]
                                self.assertEqual(reply.id, 'wire-ok')
                        self.assertNotIn(key, '\n'.join(logs.output))

    def test_compressed_json_and_sse_preserve_replies_and_close(self):
        for key in self.modes():
            for streaming in (False, True):
                with self.subTest(mode=key, streaming=streaming):
                    closed = []
                    reply = {'id': 'compressed', 'choices': [], 'quotum_billing': METER['receipts'][0]}
                    wire = (('data: ' + json.dumps(reply) + '\n\ndata: [DONE]\n\n') if streaming else json.dumps(reply)).encode()
                    class Compressed(httpx.SyncByteStream):
                        def __iter__(self):
                            compressed = gzip.compress(wire)
                            for n in range(0, len(compressed), 3):
                                yield compressed[n:n + 3]
                        def close(self):
                            closed.append(True)
                    client = self.sdk_client(lambda req: httpx.Response(200, headers={
                        'content-type': 'text/event-stream' if streaming else 'application/json',
                        'content-encoding': 'gzip'}, stream=Compressed()))
                    result = client.chat.completions.create(model='chosen', messages=[], stream=streaming)
                    if streaming:
                        with result:
                            result = list(result)[0]
                    self.assertEqual(result.id, 'compressed')
                    self.assertEqual(result.quotum_billing, METER['receipts'][0])
                    self.assertEqual(closed, [True])

    def test_usage_http_error_read_failure_keeps_safe_hint(self):
        self.payg()
        for status, phrase in ((401, 'create a new'), (402, 'top up')):
            with self.subTest(status=status):
                class Broken(io.BytesIO):
                    def read(self, *args):
                        raise OSError(PAYG_KEY)
                error = urllib.error.HTTPError(self.plugin.PAYG_BASE + '/meter', status, 'fixture', {}, Broken())
                self.json_routes({self.plugin.PAYG_BASE + '/meter': error})
                result = self.plugin.quotum.fetch_account_usage()
                self.assertIn(phrase, result.unavailable_reason)
                self.assertNotIn(PAYG_KEY, str(result))
                self.assertTrue(error.closed)

    def test_payg_unreadable_terminal_body_preserves_status_and_single_attempt(self):
        self.payg()
        for status in (401, 402):
            with self.subTest(status=status):
                calls, closed = [], []
                class Broken(httpx.SyncByteStream):
                    def __iter__(self):
                        raise httpx.ReadError(PAYG_KEY)
                        yield b''
                    def close(self):
                        closed.append(True)
                def handler(req):
                    calls.append(req)
                    return httpx.Response(status, stream=Broken())
                client = self.sdk_client(handler)
                with self.assertLogs('openai', level='DEBUG') as logs, self.assertRaises(openai.APIStatusError) as caught:
                    client.chat.completions.create(model='chosen', messages=[])
                self.assertEqual(caught.exception.status_code, status)
                self.assertEqual(len(calls), 1)
                self.assertEqual(closed, [True])
                self.assertNotIn(PAYG_KEY, ''.join(traceback.format_exception(caught.exception)) + '\n'.join(logs.output))
                verdict = self.plugin.classify_error(status_code=status, body=caught.exception.body)
                self.assertTrue(all(verdict[k] is False for k in ('retryable', 'should_rotate_credential', 'should_fallback')))

    def test_payg_metadata_never_reflects_bearer_in_presentation_fields(self):
        self.payg()
        for field in ('project', 'receipt-id', 'receipt-model', 'status-intake'):
            with self.subTest(field=field):
                meter = copy.deepcopy(METER)
                status = {'intake': True}
                if field == 'project':
                    meter['project'] = 'Bearer ' + PAYG_KEY
                elif field.startswith('receipt-'):
                    meter['receipts'][0][field.removeprefix('receipt-')] = PAYG_KEY
                else:
                    status['intake'] = {'diagnostic': PAYG_KEY}
                self.json_routes({self.plugin.PAYG_BASE + '/meter': meter, self.plugin.PAYG_BASE + '/status': status})
                for route in (self.api.account, self.api.record, self.api.payg_account):
                    self.assertNotIn(PAYG_KEY, json.dumps(route()))
                snapshot = self.plugin.quotum.fetch_account_usage()
                self.assertNotIn(PAYG_KEY, json.dumps(dataclasses.asdict(snapshot), default=str))

    def test_seat_metadata_never_reflects_the_actual_key(self):
        # The explicitly supplied inference key may differ from the process key.
        key = 'fixture-pooled-key'
        self.json_routes({self.plugin.SITE + '/api/meter/' + self.plugin.key_hash(key):
                          {'seat': 3, 'session': key, 'debug': {'Authorization': 'Bearer ' + key}},
                          self.plugin.SITE + '/api/meter/' + self.plugin.key_hash(SEAT_KEY): {'seat': 3},
                          self.plugin.SITE + '/api/record?seat=3':
                          {'record': {'seals': {}, 'counts': {}}, 'diagnostic': {'Authorization': SEAT_KEY}}})
        snapshot = self.plugin.quotum.fetch_account_usage(api_key=key)
        self.assertNotIn(key, json.dumps(dataclasses.asdict(snapshot), default=str))
        for route in (self.api.account, self.api.record):
            self.assertNotIn(SEAT_KEY, json.dumps(route()))

    def test_preflight_metadata_cannot_reflect_key_in_warning(self):
        os.environ['QUOTUM_PREFLIGHT'] = '1'
        rows = [{'id': 'chosen', 'in': 10, 'out': 10, 'privacy': 'private'},
                {'id': SEAT_KEY, 'in': 1, 'out': 1, 'privacy': 'private'}]
        with patch.object(self.plugin, 'catalog', return_value=rows), patch.object(self.plugin, 'meter', return_value={'leftUsd': 0}), self.assertLogs(self.plugin.logger, level='WARNING') as logs:
            self.plugin.preflight(SEAT_KEY, 'chosen', 'fixture-session')
        self.assertNotIn(SEAT_KEY, '\n'.join(logs.output))

    def test_payg_error_envelope_cannot_masquerade_as_balance(self):
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + '/meter': {**METER, 'error': {'message': PAYG_KEY}},
                          self.plugin.PAYG_BASE + '/status': {'intake': True}})
        self.assertIsInstance(self.api.account()['error'], str)
        snapshot = self.plugin.quotum.fetch_account_usage()
        self.assertIsNotNone(snapshot.unavailable_reason)
        self.assertNotIn(PAYG_KEY, str(snapshot))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(BoundaryTests(name) for name in sorted(BoundaryTests.__dict__) if name.startswith('test_'))
