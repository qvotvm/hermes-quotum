"""offline secrets-proxy meter regressions."""
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import openai
import test_modes as fixtures

SEAT_KEY = fixtures.SEAT_KEY


class MeterHandleTests(fixtures.ModesTest):
    def setUp(self):
        super().setUp()
        self.plugin._handle_module._INVALID_WARNED = False

    def test_override_across_usage_cap_preflight_and_dashboard(self):
        p = self.plugin
        os.environ['QUOTUM_METER_HANDLE'] = '0123456789abcdef'
        os.environ['QUOTUM_PREFLIGHT'] = '1'
        self.json_routes({p.SITE + '/api/meter/0123456789abcdef': {
            'seat': 3, 'capUsd': 2, 'spentUsd': 1.99, 'leftUsd': 0.01,
            'bellAt': '2099-11-12T20:00:00Z'},
            p.SITE + '/api/record?seat=3': {}})
        snapshot = p.quotum.fetch_account_usage(api_key='ibsec_synthetic')
        self.assertEqual(snapshot.windows[0].used_percent, 99.5)
        self.assertIn('2099-11-12 20:00 UTC', p.cap_spent_message('ibsec_synthetic'))
        with patch.object(p, 'catalog', return_value=[
            {'id': 'large', 'privacy': 'private', 'in': 10, 'out': 10},
            {'id': 'small', 'privacy': 'private', 'in': 0.1, 'out': 0.1}]), self.assertLogs(p.logger, level='WARNING'):
            p.preflight('ibsec_synthetic', 'large', 's')
        self.assertEqual(self.api.record()['seat'], 3)
        meter_urls = [r.full_url for r in self.seen if '/meter/' in r.full_url]
        self.assertEqual(meter_urls, [p.SITE + '/api/meter/0123456789abcdef'] * 2)
        self.assertEqual(set(p._METER_CACHE), {'0123456789abcdef'})
        self.assertEqual(p.meter_handle(SEAT_KEY), '0123456789abcdef')
        # a handle change must invalidate the dashboard's selected identity.
        os.environ['QUOTUM_METER_HANDLE'] = 'fedcba9876543210'
        with patch.object(self.api, '_read_record', return_value={'seat': 4}) as read:
            self.assertEqual(self.api.record()['seat'], 4)
            read.assert_called_once()

    def test_invalid_handles_ignored_with_one_private_warning(self):
        p = self.plugin
        with self.assertLogs('quotum.meter_handle', level='WARNING') as logs:
            for value in ('', '0123456789abcde', '0123456789abcdef0', '0123456789abcdeF',
                          ' 0123456789abcdef', '0123456789abcdef\n', 'synthetic-private-value'):
                os.environ['QUOTUM_METER_HANDLE'] = value
                self.assertEqual(p.meter_handle(SEAT_KEY), p.key_hash(SEAT_KEY))
                self.assertIs(p._handle_module, self.api._handle_module)
                self.assertEqual(self.api._meter_handle(SEAT_KEY), p.key_hash(SEAT_KEY))
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn('synthetic-private-value', logs.output[0])
        with patch.object(p, '_get_json', return_value={'leftUsd': 1}) as get:
            self.assertEqual(p.meter(SEAT_KEY)['leftUsd'], 1)
        get.assert_called_once_with(p.SITE + '/api/meter/' + p.key_hash(SEAT_KEY))

    def test_proxy_without_handle_guidance_and_fail_open(self):
        p = self.plugin
        proxy = 'ibsec_synthetic'
        os.environ['QUOTUM_SEAT_KEY'] = proxy
        os.environ['QUOTUM_PREFLIGHT'] = '1'
        with patch.object(p, '_get_json') as get, patch.object(self.api, '_get') as dashboard_get:
            self.assertIsNone(p.meter(proxy))
            self.assertEqual(p.quotum.fetch_account_usage(api_key=proxy).unavailable_reason, p.PROXY_MESSAGE)
            self.assertEqual(p.cap_spent_message(proxy), p.PROXY_MESSAGE)
            self.assertEqual(self.api.record()['error'], p.PROXY_MESSAGE)
            p.quotum.build_extra_body(session_id='s', model='large')
            sent = []
            def respond(request):
                sent.append(request)
                return httpx.Response(200, json={'id': 'ok', 'choices': []})
            self.start(patch('httpx.HTTPTransport.handle_request', side_effect=httpx.MockTransport(respond).handle_request))
            client = p.quotum.create_client(api_key=proxy, base_url=p.VENICE_BASE, max_retries=0)
            self.addCleanup(client.close)
            with self.assertLogs(p.logger, level='WARNING') as logs:
                self.assertEqual(client.chat.completions.create(model='large', messages=[]).id, 'ok')
                p.preflight(proxy, 'large', 's')
            self.assertEqual(logs.output, ['WARNING:' + p.logger.name + ':' + p.PROXY_MESSAGE])
            self.assertEqual(sent[0].headers['authorization'], 'Bearer ' + proxy)
            # exercise the proxy marker on the actual transport, preserving the proxy marker.
            with p.quotum.create_client(api_key=proxy, base_url=p.VENICE_BASE, max_retries=0) as direct:
                self.assertEqual(direct.chat.completions.create(model='large', messages=[]).id, 'ok')
            self.assertEqual(sent[-1].headers['authorization'], 'Bearer ' + proxy)
            get.assert_not_called()
            dashboard_get.assert_not_called()
        self.assertEqual(p._METER_CACHE, {})

    def test_proxy_sdk_402_guidance_stays_terminal(self):
        p = self.plugin
        sent = []
        def respond(request):
            sent.append(request)
            return httpx.Response(402, json={'error': {'message': 'upstream detail'}})
        self.start(patch('httpx.HTTPTransport.handle_request', side_effect=httpx.MockTransport(respond).handle_request))
        with patch.object(p, '_get_json') as get:
            with p.quotum.create_client(api_key='ibsec_synthetic', base_url=p.VENICE_BASE, max_retries=3) as client:
                with self.assertRaises(openai.APIStatusError) as raised:
                    client.chat.completions.create(model='large', messages=[])
            self.assertEqual(raised.exception.body['message'], p.PROXY_MESSAGE)
            self.assertEqual(len(sent), 1)
            self.assertNotIn('upstream detail', str(raised.exception))
            get.assert_not_called()

    def test_proxy_invalid_handle_still_has_guidance(self):
        os.environ['QUOTUM_METER_HANDLE'] = 'INVALID'
        with self.assertLogs('quotum.meter_handle', level='WARNING'):
            self.assertEqual(self.plugin.cap_spent_message('ibsec_synthetic'), self.plugin.PROXY_MESSAGE)

    def test_normal_key_unchanged(self):
        expected = hashlib.sha256(SEAT_KEY.encode()).hexdigest()[:16]
        self.assertEqual(self.plugin.meter_handle(SEAT_KEY), expected)
        with patch.object(self.plugin, '_get_json', return_value={'leftUsd': 1}) as get:
            self.plugin.meter(SEAT_KEY)
        get.assert_called_once_with(self.plugin.SITE + '/api/meter/' + expected)

    def test_handle_uses_active_profile_without_process_fallback(self):
        p = self.plugin
        os.environ['QUOTUM_METER_HANDLE'] = '0123456789abcdef'
        scope = sys.modules['agent.secret_scope']
        for configured, expected in ((None, p.key_hash(SEAT_KEY)),
                                     ('fedcba9876543210', 'fedcba9876543210')):
            values = {'QUOTUM_SEAT_KEY': SEAT_KEY, 'QUOTUM_METER_HANDLE': configured}
            with patch.object(scope, 'get_secret', side_effect=values.get), \
                    patch.object(os.environ, 'get', side_effect=AssertionError('process environment forbidden')):
                self.assertEqual(p.meter_handle(SEAT_KEY), expected)
                self.assertEqual(self.api._meter_handle(SEAT_KEY), expected)
                self.assertEqual(p.meter_handle('ibsec_synthetic'), configured)

    def test_dashboard_cannot_reuse_handle_cache_after_key_removal_or_rotation(self):
        os.environ['QUOTUM_METER_HANDLE'] = '0123456789abcdef'
        with patch.object(self.api, '_read_record', return_value={'seat': 3}) as read:
            self.assertEqual(self.api.record()['seat'], 3)
            os.environ['QUOTUM_SEAT_KEY'] = 'another-synthetic-key'
            self.assertEqual(self.api.record()['seat'], 3)
            self.assertEqual(read.call_count, 2)
        del os.environ['QUOTUM_SEAT_KEY']
        with patch.object(self.api, '_get') as get:
            self.assertIn('set QUOTUM_SEAT_KEY', self.api.record()['error'])
            get.assert_not_called()
        self.assertIsNone(self.plugin.meter_handle(''))
        self.assertNotIn(SEAT_KEY, repr(self.api._CACHE))
        self.assertNotIn('another-synthetic-key', repr(self.api._CACHE))

    def test_invalid_warning_is_once_across_concurrent_provider_and_dashboard(self):
        os.environ['QUOTUM_METER_HANDLE'] = 'synthetic-invalid-value'
        with self.assertLogs('quotum.meter_handle', level='WARNING') as logs:
            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(lambda n: (self.plugin.meter_handle if n % 2 else self.api._meter_handle)(SEAT_KEY), range(96)))
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn('synthetic-invalid-value', logs.output[0])

    def test_proxy_metadata_and_x_search_send_no_bearer(self):
        p = self.plugin
        proxy = 'ibsec_synthetic'
        os.environ.update(QUOTUM_SEAT_KEY=proxy, QUOTUM_METER_HANDLE='0123456789abcdef', QUOTUM_X_SEARCH='1')
        self.json_routes({
            p.SITE + '/api/seat/models': {'models': [{'id': 'grok', 'privacy': 'private'}]},
            p.SITE + '/api/meter/0123456789abcdef': {'seat': 3, 'capUsd': 1, 'spentUsd': 0, 'leftUsd': 1},
            p.SITE + '/api/record?seat=3': {},
            p.VENICE_BASE + '/models?type=text': {'data': []}})
        p.quotum.fetch_models(api_key=proxy)
        p.quotum.fetch_account_usage(api_key=proxy)
        p.x_search_models()
        self.api.record()
        self.assertEqual(len(self.seen), 5)
        for request in self.seen:
            self.assertNotIn('authorization', {k.lower() for k in request.headers})
            self.assertNotIn(proxy, request.full_url + repr(request.headers))

    def test_proxy_inference_rejects_other_hosts_before_transport(self):
        p = self.plugin
        with patch('httpx.HTTPTransport.handle_request') as send:
            for base in ('https://quotum.org/api/v1', 'https://api.venice.ai.evil.test/api/v1',
                         'http://api.venice.ai/api/v1', 'https://api.venice.ai:444/api/v1'):
                with p.quotum.create_client(api_key='ibsec_synthetic', base_url=base, max_retries=0) as client:
                    with self.assertRaises(openai.APIConnectionError):
                        client.chat.completions.create(model='large', messages=[])
            send.assert_not_called()
