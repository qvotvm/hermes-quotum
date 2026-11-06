"""Catalog rule 11: only the host's active secret scope supplies credentials."""
import os
import sys
import types
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_modes import ModesTest, PAYG_KEY, ROOT, SEAT_KEY, load
Base = ModesTest
del ModesTest


class ScopeTests(Base):
    def unavailable_scopes(self):
        return (None, types.ModuleType('agent.secret_scope'),
                types.SimpleNamespace(get_secret=None), types.SimpleNamespace(get_secret=0))

    def test_missing_scope_never_reads_environment_or_credential_files(self):
        for scope in self.unavailable_scopes():
            with self.subTest(scope=scope), patch.dict(sys.modules, {'agent.secret_scope': scope}):
                for helper in (self.plugin.payg, self.api.payg_helpers):
                    with patch.object(os.environ, 'get', side_effect=AssertionError('environment read forbidden')), \
                            patch('builtins.open', side_effect=AssertionError('credential file read forbidden')):
                        for name in ('QUOTUM_SEAT_KEY', 'QUOTUM_PAYG_KEY', 'QUOTUM_WALLET'):
                            with self.assertRaisesRegex(RuntimeError, 'upgrade Hermes'):
                                helper.secret(name)

    def test_older_host_imports_and_provider_actions_give_upgrade_error(self):
        for scope in self.unavailable_scopes():
            with self.subTest(scope=scope), patch.dict(sys.modules, {'agent.secret_scope': scope}), \
                    patch.dict(os.environ, {'QUOTUM_PAYG_KEY': PAYG_KEY, 'QUOTUM_SEAT_KEY': SEAT_KEY}):
                plugin = load('quotum_legacy_scope_test', ROOT / '__init__.py', True)
                api = load('quotum_legacy_dashboard_test', ROOT / 'dashboard/plugin_api.py')
                self.assertEqual(plugin.quotum.base_url, plugin.VENICE_BASE)
                actions = (
                    plugin.payg_key, plugin.quotum.fetch_models,
                    plugin.quotum.build_extra_body, plugin.quotum.fetch_account_usage,
                    lambda: plugin.quotum.get_max_tokens('chosen'),
                    lambda: plugin.quotum.create_client(api_key=SEAT_KEY, base_url=plugin.VENICE_BASE),
                    lambda: plugin.classify_error(status_code=401),
                )
                with patch.object(plugin, '_get_json') as fetch:
                    for action in actions:
                        with self.assertRaisesRegex(RuntimeError, 'upgrade Hermes') as caught:
                            action()
                        self.assertNotIn(SEAT_KEY, str(caught.exception))
                        self.assertNotIn(PAYG_KEY, str(caught.exception))
                    fetch.assert_not_called()
                self.assertIn('upgrade Hermes', api.account()['error'])

    def test_older_dashboard_routes_return_local_upgrade_message_without_io(self):
        app = FastAPI()
        app.include_router(self.api.router, prefix='/api/plugins/quotum')
        for scope in self.unavailable_scopes():
            with self.subTest(scope=scope), patch.dict(sys.modules, {'agent.secret_scope': scope}), \
                    patch.dict(os.environ, {'QUOTUM_PAYG_KEY': PAYG_KEY}), \
                    patch.object(self.api, '_get') as fetch, TestClient(app) as client:
                for route in ('account', 'record', 'payg'):
                    response = client.get('/api/plugins/quotum/' + route)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn('upgrade Hermes', response.json()['error'])
                    self.assertNotIn(SEAT_KEY, response.text)
                    self.assertNotIn(PAYG_KEY, response.text)
                fetch.assert_not_called()

    def test_scope_switch_and_empty_profile_never_borrow_process_keys(self):
        scope = sys.modules['agent.secret_scope']
        with patch.dict(os.environ, {'QUOTUM_SEAT_KEY': SEAT_KEY, 'QUOTUM_PAYG_KEY': PAYG_KEY}):
            for values in ({'QUOTUM_PAYG_KEY': ' scoped-project '}, {'QUOTUM_SEAT_KEY': ' scoped-seat '}, {}):
                with self.subTest(values=values), patch.object(scope, 'get_secret', side_effect=values.get), \
                        patch.object(os.environ, 'get', side_effect=AssertionError('environment read forbidden')):
                    for helper in (self.plugin.payg, self.api.payg_helpers):
                        for name in ('QUOTUM_PAYG_KEY', 'QUOTUM_SEAT_KEY'):
                            self.assertEqual(helper.secret(name), values.get(name, '').strip())
                    self.assertEqual(self.plugin.payg_key(), values.get('QUOTUM_PAYG_KEY', '').strip())


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ScopeTests(name) for name in sorted(ScopeTests.__dict__) if name.startswith('test_'))
