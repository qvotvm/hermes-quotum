"""Offline model-health regressions from public Venice captures and local peers."""
import copy
import io
import json
import os
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from test_modes import ModesTest, PAYG_KEY, ROOT, SEAT_KEY
Base = ModesTest
del ModesTest


class ModelHealthTests(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop('QUOTUM_MODEL_HEALTH')
        self.health = self.plugin.model_health
        self.health._CACHE.clear()
        self.health._WARNED.clear()
        self.health._UNAVAILABLE_UNTIL = 0
        self.index = json.loads((ROOT / 'tests/fixtures/metrics-index.json').read_text())
        self.detail = json.loads((ROOT / 'tests/fixtures/detail-aion35.json').read_text())
        self.row = next(r for r in self.index['models'] if r['id'] == 'aion-labs-aion-3-5')
        self.start(patch('socket.create_connection', side_effect=AssertionError('offline test')))
        self.start(patch('socket.socket.connect', side_effect=AssertionError('offline test')))

    def routes(self, rows=None, details=None):
        self.json_routes({self.health.INDEX: {'models': rows if rows is not None else self.index['models']},
                          **{self.health.BASE + path: detail for path, detail in (details or {}).items()}})

    def model(self, name, **changes):
        return {**copy.deepcopy(self.row), 'id': name, 'detail_path': 'models/' + name + '.json', **changes}

    def setup_route(self, *, fallback=None):
        os.environ.update(QUOTUM_MODEL_HEALTH='route', QUOTUM_HEALTH_FALLBACK='missing, fallback')
        rows = [self.model('chosen', uptime_24h_percent=90), self.model('fallback')]
        self.routes(rows, {r['detail_path']: fallback if fallback is not None and r['id'] == 'fallback' else self.detail for r in rows})
        return self.start(patch.object(self.plugin, 'catalog', return_value=[{'id': 'fallback', 'privacy': 'private'}]))

    def request(self, model='chosen', *, stream=False):
        return httpx.Request('POST', self.plugin.VENICE_BASE + '/chat/completions',
                             content=json.dumps({'model': model, 'messages': [], 'stream': stream}).encode())

    def test_captured_aion_health_and_null_bucket_counts(self):
        buckets = self.detail['uptime']['buckets']
        self.assertEqual(len(buckets), 96)
        self.assertEqual(sum(b['status'] == 'up' and b['uptime_percent'] is None for b in buckets), 21)
        self.assertEqual(sum(b['status'] == 'degraded' and b['uptime_percent'] is None for b in buckets), 1)
        self.assertEqual(self.health.assess(self.row, self.detail).state, 'healthy')
        buckets[-2:] = [{'status': 'up', 'uptime_percent': None}] * 2
        result = self.health.assess(self.row, self.detail)
        self.assertEqual(result.state, 'unknown')
        self.assertIn('no data', result.reason)

    def test_captured_index_screen_counts_before_detail_checks(self):
        # The other 147 detail files are not in the capture; this is an index-only screen.
        states = [self.health.assess(row, self.detail).state for row in self.index['models']]
        self.assertEqual(len(states), 148)
        self.assertEqual({state: states.count(state) for state in ('healthy', 'unhealthy', 'unknown')},
                         {'healthy': 111, 'unhealthy': 37, 'unknown': 0})

    def test_catalog_privacy_filter_applies_to_real_route(self):
        os.environ.update(QUOTUM_MODEL_HEALTH='route', QUOTUM_HEALTH_FALLBACK='public, offline, fallback')
        rows = [self.model('chosen', uptime_24h_percent=90), self.model('public'), self.model('offline'), self.model('fallback')]
        self.routes(rows, {r['detail_path']: self.detail for r in rows})
        routes = {self.health.INDEX: {'models': rows},
                  **{self.health.BASE + r['detail_path']: self.detail for r in rows},
                  self.plugin.SITE + '/api/seat/models': {'models': [
                      {'id': 'public', 'privacy': 'public'}, {'id': 'offline', 'privacy': 'private', 'offline': True},
                      {'id': 'fallback', 'privacy': 'anonymized'}]}}
        self.json_routes(routes)
        routed, _ = self.plugin._health_request(self.request(), SEAT_KEY)
        self.assertEqual(json.loads(routed.content)['model'], 'fallback')
        self.assertEqual(len(self.seen), 4)
        self.assertFalse(any(r.full_url.endswith(('/public.json', '/offline.json')) for r in self.seen))

    def test_malformed_detail_is_cached_unavailable(self):
        row = self.model('chosen')
        for detail in ({}, [], {'uptime': {'buckets': [None]}}):
            self.health._CACHE.clear()
            self.health._UNAVAILABLE_UNTIL = 0
            self.routes([row], {row['detail_path']: detail})
            with patch.object(self.health.time, 'monotonic', return_value=100):
                for _ in range(2):
                    original = self.request()
                    routed, decision = self.plugin._health_request(original, SEAT_KEY)
                    self.assertIs(routed, original)
                    self.assertIsNone(decision)
            self.assertEqual(len(self.seen), 2)

    def test_explicit_health_rule_and_boundaries(self):
        for changes, status, state in (({}, 'up', 'healthy'), ({'uptime_24h_percent': 97}, 'up', 'healthy'),
                ({'uptime_24h_percent': 96.99}, 'up', 'unhealthy'), ({'uptime_24h_percent': None}, 'up', 'unknown'),
                ({}, 'degraded', 'unhealthy'), ({}, 'down', 'unhealthy'),
                ({'performance': {'1h': {'ttft_ms': {'p90': 15000}}}}, 'up', 'healthy'),
                ({'performance': {'1h': {'ttft_ms': {'p90': 15001}}}}, 'up', 'unhealthy')):
            with self.subTest(changes=changes, status=status):
                detail = copy.deepcopy(self.detail)
                detail['uptime']['buckets'][-1]['status'] = status
                self.assertEqual(self.health.assess({**self.row, **changes}, detail).state, state)
        self.assertEqual(self.health.assess(None, self.detail).state, 'unknown')
        for perf in (None, {}, {'1h': None}, {'1h': {'ttft_ms': {'p90': None}}}):
            result = self.health.assess({**self.row, 'performance': perf}, self.detail)
            self.assertEqual(result.state, 'healthy')
            self.assertIn('latency unknown', result.reason)

    def test_healthy_requested_unchanged_in_every_mode(self):
        self.routes([self.row], {self.row['detail_path']: self.detail})
        for mode in ('warn', 'route', 'off'):
            os.environ['QUOTUM_MODEL_HEALTH'] = mode
            original = self.request(self.row['id'])
            routed, _ = self.plugin._health_request(original, SEAT_KEY)
            self.assertIs(routed, original)
        self.assertEqual(len(self.seen), 2)

    def test_default_warn_once_per_model_per_cache_window(self):
        row = self.model('chosen', uptime_24h_percent=90)
        self.routes([row], {row['detail_path']: self.detail})
        original = self.request()
        with patch.object(self.health.time, 'monotonic', return_value=100), self.assertLogs(self.plugin.logger, level='INFO') as logs:
            for _ in range(3):
                self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)
        self.assertEqual(len(logs.output), 1)
        with patch.object(self.health.time, 'monotonic', return_value=400), self.assertLogs(self.plugin.logger, level='INFO') as logs:
            self.plugin._health_request(original, SEAT_KEY)
        self.assertEqual(len(logs.output), 1)
        self.assertEqual(len(self.seen), 4)

    def test_route_down_model_and_ordered_catalog_fallback(self):
        self.setup_route()
        with self.assertLogs(self.plugin.logger, level='INFO') as logs:
            routed, decision = self.plugin._health_request(self.request(), SEAT_KEY)
        self.assertEqual(json.loads(routed.content)['model'], 'fallback')
        self.assertEqual(decision['requested'], 'chosen')
        self.assertEqual(len(logs.output), 1)
        for text in ('chosen', 'fallback', 'uptime'):
            self.assertIn(text, logs.output[0])
        self.assertEqual(len(self.seen), 3)

    def test_warn_never_uses_fallback(self):
        catalog = self.setup_route()
        os.environ['QUOTUM_MODEL_HEALTH'] = 'warn'
        original = self.request()
        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)
        catalog.assert_not_called()
        self.assertEqual(len(self.seen), 2)

    def test_unknown_null_buckets_never_selected_as_fallback(self):
        detail = copy.deepcopy(self.detail)
        detail['uptime']['buckets'][-2:] = [{'status': 'up', 'uptime_percent': None}] * 2
        self.setup_route(fallback=detail)
        original = self.request()
        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)

    def test_unknown_requested_can_route(self):
        self.setup_route()
        routed, decision = self.plugin._health_request(self.request('absent'), SEAT_KEY)
        self.assertEqual(decision['state'], 'unknown')
        self.assertEqual(json.loads(routed.content)['model'], 'fallback')

    def test_fallback_not_in_catalog_skipped_before_detail_fetch(self):
        catalog = self.setup_route()
        catalog.return_value = []
        original = self.request()
        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)
        self.assertEqual(len(self.seen), 2)

    def test_no_config_or_no_healthy_fallback_preserves_model(self):
        catalog = self.setup_route()
        os.environ['QUOTUM_HEALTH_FALLBACK'] = ''
        original = self.request()
        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)
        catalog.assert_not_called()
        os.environ['QUOTUM_HEALTH_FALLBACK'] = 'missing'
        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)

    def test_fallback_order_skips_unhealthy_and_unknown(self):
        self.setup_route()
        os.environ['QUOTUM_HEALTH_FALLBACK'] = 'chosen, absent, fallback'
        routed, _ = self.plugin._health_request(self.request(), SEAT_KEY)
        self.assertEqual(json.loads(routed.content)['model'], 'fallback')

    def test_failures_cached_for_60_seconds_debug_only(self):
        for error in (OSError(SEAT_KEY), TimeoutError(PAYG_KEY), json.JSONDecodeError('bad', '', 0)):
            with self.subTest(error=type(error).__name__):
                self.health._CACHE.clear()
                self.health._UNAVAILABLE_UNTIL = 0
                with patch.object(self.health, '_get_json', side_effect=error) as fetch, \
                        patch.object(self.health.time, 'monotonic', return_value=100), \
                        self.assertLogs(self.health.logger, level='DEBUG') as logs:
                    for _ in range(3):
                        original = self.request()
                        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)
                    self.assertEqual(fetch.call_count, 1)
                self.assertEqual(len(logs.output), 1)
                self.assertTrue(all(':DEBUG:' in line or line.startswith('DEBUG:') for line in logs.output))
                self.assertNotIn(SEAT_KEY, str(logs.output))
                self.assertNotIn(PAYG_KEY, str(logs.output))
                with patch.object(self.health, '_get_json', side_effect=error) as fetch:
                    with patch.object(self.health.time, 'monotonic', return_value=159.9):
                        self.plugin._health_request(self.request(), SEAT_KEY)
                        fetch.assert_not_called()
                    with patch.object(self.health.time, 'monotonic', return_value=160):
                        self.plugin._health_request(self.request(), SEAT_KEY)
                        self.assertEqual(fetch.call_count, 1)

    def test_non_json_response_and_invalid_shapes_fail_open(self):
        for raw in (b'not json', b'[]', b'{}', b'{"models":[null]}'):
            self.health._UNAVAILABLE_UNTIL = 0
            with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=lambda *a, **kw: io.BytesIO(raw))):
                original = self.request()
                self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)

    def test_detail_failure_blocks_routing_for_host_failure_window(self):
        self.setup_route()
        self.seen.clear()
        fetch = self.health._get_json
        def read(url):
            if url.endswith('/fallback.json'):
                raise TimeoutError('fixture')
            return fetch(url)
        with patch.object(self.health, '_get_json', side_effect=read), patch.object(self.health.time, 'monotonic', return_value=100):
            for _ in range(3):
                original = self.request()
                routed, decision = self.plugin._health_request(original, SEAT_KEY)
                self.assertIs(routed, original)
                self.assertIsNone(decision)
        self.assertEqual(len(self.seen), 2)

    def test_cache_ttl_respected_and_only_checked_details_read(self):
        os.environ['QUOTUM_METRICS_CACHE_SECONDS'] = '10'
        self.routes([self.row], {self.row['detail_path']: self.detail})
        for stamp, count in ((100, 2), (109.9, 2), (110, 4)):
            with patch.object(self.health.time, 'monotonic', return_value=stamp):
                self.assertEqual(self.health.check(self.row['id']).state, 'healthy')
                self.assertEqual(len(self.seen), count)
        for value in ('0', '-1', 'nan', 'inf', 'bad'):
            os.environ['QUOTUM_METRICS_CACHE_SECONDS'] = value
            self.assertEqual(self.health.cache_seconds(), 300)

    def test_off_no_fetch_or_catalog(self):
        os.environ['QUOTUM_MODEL_HEALTH'] = 'off'
        with patch.object(self.health, '_get_json') as fetch, patch.object(self.plugin, 'catalog') as catalog:
            self.plugin._health_request(self.request(), SEAT_KEY)
            fetch.assert_not_called()
            catalog.assert_not_called()

    def test_metrics_headers_keyless_timeout_and_no_proxy(self):
        self.payg()
        os.environ['UNRELATED_SECRET'] = 'fixture-environment-value'
        calls = []
        def send(request, **kwargs):
            calls.append((request, kwargs))
            return io.BytesIO(b'{"models":[]}')
        with patch('urllib.request.build_opener', return_value=SimpleNamespace(open=send)) as build:
            self.health._get_json(self.health.INDEX)
        request, kwargs = calls[0]
        self.assertEqual(request.headers, {'Accept': 'application/json'})
        self.assertIsNone(request.data)
        self.assertEqual(kwargs, {'timeout': 2.0})
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], self.health._NoRedirect)
        for key in (SEAT_KEY, PAYG_KEY, os.environ['UNRELATED_SECRET']):
            self.assertNotIn(key, request.full_url + repr(request.headers))

    def test_wrong_hosts_paths_schemes_and_redirects_refused(self):
        with patch('urllib.request.build_opener') as build:
            for url in ('http://metrics.venice.ai/v1/index.json', 'https://evil.test/v1/index.json',
                        'https://metrics.venice.ai.evil.test/v1/index.json', 'https://metrics.venice.ai:444/v1/index.json',
                        self.health.BASE + '../index.json', self.health.INDEX + '?key=x',
                        self.health.BASE + 'models/../../evil.json', self.health.BASE + 'models/x.json#fragment'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    self.health._get_json(url)
            build.assert_not_called()
        for status in (301, 302, 303, 307, 308):
            with self.assertRaises(urllib.error.HTTPError):
                self.health._NoRedirect().redirect_request(SimpleNamespace(full_url=self.health.INDEX), None,
                                                           status, 'redirect', {}, self.health.INDEX)
        self.routes([self.model('chosen', detail_path='https://evil.test/model.json')])
        original = self.request()
        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)
        self.assertEqual(len(self.seen), 1)

    def test_streaming_rewrite_preserves_every_other_byte_both_transports(self):
        self.setup_route()
        captured = []
        def respond(request):
            captured.append(request)
            return httpx.Response(200, json={'id': 'ok', 'choices': []})
        self.start(patch('httpx.HTTPTransport.handle_request', side_effect=httpx.MockTransport(respond).handle_request))
        for payg_mode in (False, True):
            os.environ['QUOTUM_PAYG_KEY'] = PAYG_KEY if payg_mode else ''
            with self.plugin.quotum.create_client(api_key=SEAT_KEY, base_url=self.plugin.VENICE_BASE, max_retries=0) as client:
                for streaming in (False, True):
                    raw = (' {\n "messages": [{"content":"caf\\u00e9 \\"model\\": nested", "model":"nested"}], '
                           '"mo\\u0064el" : "chosen", "stream": ' + str(streaming).lower() + ', "n":1.00 } \n').encode()
                    response = client._client.send(httpx.Request('POST', str(client.base_url) + 'chat/completions',
                        headers={'authorization': 'Bearer ' + (PAYG_KEY if payg_mode else SEAT_KEY), 'x-test': 'kept'}, content=raw))
                    self.assertEqual(captured[-1].content, raw.replace(b'"chosen"', b'"fallback"'))
                    self.assertEqual(captured[-1].headers['content-length'], str(len(captured[-1].content)))
                    self.assertEqual(captured[-1].headers['x-test'], 'kept')
                    self.assertEqual(response.headers['x-quotum-model-from'], 'chosen')
                    self.assertEqual(response.headers['x-quotum-model-to'], 'fallback')
                    self.assertEqual(response.extensions['quotum_model_health']['state'], 'unhealthy')
                    self.assertEqual(captured[-1].headers['authorization'], 'Bearer ' + (PAYG_KEY if payg_mode else SEAT_KEY))

    def test_duplicate_model_fields_fail_open(self):
        self.setup_route()
        original = httpx.Request('POST', self.plugin.VENICE_BASE + '/chat/completions', content=b'{"model":"chosen","model":"chosen"}')
        self.assertIs(self.plugin._health_request(original, SEAT_KEY)[0], original)

    def test_nonchat_requests_do_not_fetch_metrics(self):
        with patch.object(self.health, '_get_json') as fetch:
            client = self.sdk_client(lambda req: httpx.Response(200, json={'data': []}))
            client.models.list()
            fetch.assert_not_called()

    def test_routed_x_search_uses_final_model(self):
        self.setup_route()
        os.environ['QUOTUM_X_SEARCH'] = '1'
        for supported in (set(), {'fallback'}):
            self.plugin._X_MODELS = supported
            request = httpx.Request('POST', self.plugin.VENICE_BASE + '/chat/completions', json={
                'model': 'chosen', 'messages': [], 'venice_parameters': {'include_venice_system_prompt': False, 'enable_x_search': True},
                'tools': [{'function': {'name': 'web_search'}}]})
            routed, _ = self.plugin._health_request(request, SEAT_KEY)
            body = json.loads(routed.content)
            self.assertEqual(bool(body['venice_parameters'].get('enable_x_search')), bool(supported))
            self.assertFalse(body['venice_parameters']['include_venice_system_prompt'])
            filtered = self.plugin._without_web_search(routed.content)
            self.assertEqual(filtered is not None, bool(supported))

    def test_model_credentials_and_log_injection_never_enter_metrics(self):
        with patch.object(self.health, '_get_json') as fetch:
            for model in (SEAT_KEY, PAYG_KEY, 'forged\nline', {'bad': True}):
                request = self.request(model)
                self.assertIs(self.plugin._health_request(request, PAYG_KEY)[0], request)
            fetch.assert_not_called()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ModelHealthTests(name) for name in sorted(ModelHealthTests.__dict__) if name.startswith('test_'))
