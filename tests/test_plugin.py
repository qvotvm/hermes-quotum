"""Offline regression tests against the installed Hermes provider API and OpenAI SDK."""
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SOURCE = Path(os.environ.get("HERMES_SOURCE", "~/.hermes/hermes-agent")).expanduser()
sys.path.insert(0, str(SOURCE))
import httpx
import openai
from agent import error_classifier

spec = importlib.util.spec_from_file_location("quotum_plugin_test", Path(__file__).resolve().parents[1] / "__init__.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
with patch("providers.register_provider"):
    spec.loader.exec_module(plugin)

ROWS = [
    {"id": "expensive", "privacy": "anonymized", "in": 10, "out": 30},
    {"id": "private-small", "privacy": "private", "in": 0.1, "out": 0.3},
    {"id": "anon-cheapest", "privacy": "anonymized", "in": 0, "out": 0},
    {"id": "private-offline", "privacy": "private", "offline": True, "in": 0, "out": 0},
]


class PluginTests(unittest.TestCase):
    def setUp(self):
        plugin._METER_CACHE.clear()
        plugin._CATALOG_CACHE = None
        plugin._WARNED.clear()
        plugin._X_MODELS = None
        plugin._SESSION.set(None)
        self.env = patch.dict(os.environ, {"QUOTUM_PAYG_KEY": "", "QUOTUM_PREFLIGHT": "0", "QUOTUM_X_SEARCH": "0", "QUOTUM_PREFLIGHT_MINUTES": "5"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_real_hermes_classifier_terminal_flags(self):
        for status, reason in [(402, "billing"), (401, "auth_permanent")]:
            ctx = SimpleNamespace(provider_slug="quotum", error=Exception(), status_code=status,
                                  error_code=None, msg="upstream error", body={}, model="expensive", provider="quotum")
            with patch("providers.get_provider_profile", return_value=plugin.quotum):
                verdict = error_classifier._profile_verdict(ctx)
            classified = error_classifier.ClassifiedError(**verdict)
            self.assertEqual(classified.reason.value, reason)
            self.assertFalse(classified.retryable)
            self.assertFalse(classified.should_rotate_credential)
            self.assertFalse(classified.should_fallback)
        self.assertIsNone(plugin.classify_error(status_code=429))
        self.assertIsNone(plugin.classify_error(status_code=500))

    def request(self, status, *, x_search=False):
        self.sent = []
        def respond(transport, req):
            self.sent.append(req)
            return httpx.Response(status, headers={"x-should-retry": "true"}, json={"error": {"message": "raw upstream text"}}, request=req)
        with patch.object(httpx.HTTPTransport, "handle_request", respond):
            with plugin.quotum.create_client(api_key="actual-inference-key", base_url=plugin.VENICE_BASE, max_retries=3, unknown_future_kwarg=True) as client:
                with self.assertRaises(openai.APIStatusError) as raised:
                    client.chat.completions.create(model="expensive", messages=[{"role": "user", "content": "private prompt"}],
                        tools=[{"type": "function", "function": {"name": "web_search"}}, {"type": "function", "function": {"name": "other"}}],
                        extra_body={"venice_parameters": {"enable_x_search": x_search}})
        self.assertEqual(len(self.sent), 1)
        return raised.exception

    def test_real_hermes_recovery_returns_before_retry_or_fallback(self):
        from agent import turn_api_error
        classified = error_classifier.ClassifiedError(reason=error_classifier.FailoverReason.billing,
                                                       **{k: v for k, v in plugin.classify_error(status_code=402).items() if k != "reason"})
        agent = MagicMock()
        loop = SimpleNamespace(_arm_fallback_restart=MagicMock(), _credential_refresh_profile=MagicMock(return_value=None), _is_copilot_provider=lambda a: False,
                               _is_stale_copilot_credential_error=MagicMock())
        with patch.dict(sys.modules, {"agent.conversation_loop": loop}), patch.object(turn_api_error, "settle_delivered_partial", return_value=""), patch.object(turn_api_error, "nonretryable_client_error_result", return_value={"terminal": True}) as terminal, patch.object(turn_api_error, "interruptible_backoff_sleep") as sleep:
            result = turn_api_error.settle_unrecovered_error(
                agent, api_error=Exception("cap spent"), classified=classified, _retry=SimpleNamespace(provider_stale_refresh_attempted=False),
                status_code=402, error_msg="cap spent", is_context_length_error=False, is_rate_limited=False,
                _is_zai_coding_overload=False, _provider="quotum", _base=plugin.VENICE_BASE, _model="expensive",
                messages=[], api_messages=[], api_kwargs={}, active_system_prompt="", conversation_history=[],
                approx_tokens=0, retry_count=1, max_retries=10, compression_attempts=0, api_call_count=1)
        self.assertEqual(result.action, "return")
        self.assertEqual(result.result, {"terminal": True})
        terminal.assert_called_once()
        agent._try_activate_fallback.assert_not_called()
        sleep.assert_not_called()

    def test_preflight_transport_uses_actual_key_and_preserves_model(self):
        plugin.quotum.build_extra_body(session_id="session-live", model="expensive")
        def get(url, **kwargs):
            return {"models": ROWS} if url.endswith("/models") else {"leftUsd": 0.01}
        with patch.dict(os.environ, {"QUOTUM_PREFLIGHT": "1", "QUOTUM_SEAT_KEY": "different-env-key"}), patch.object(plugin, "_get_json", side_effect=get) as fetch, self.assertLogs(plugin.logger, level="WARNING"):
            self.request(401)
        self.assertEqual(json.loads(self.sent[0].content)["model"], "expensive")
        meter_urls = [call.args[0] for call in fetch.call_args_list if "/meter/" in call.args[0]]
        self.assertEqual(meter_urls, [plugin.SITE + "/api/meter/" + plugin.key_hash("actual-inference-key")])

    def test_usage_shares_meter_cache_and_preserves_snapshot(self):
        m = {"capUsd": 2, "spentUsd": 0.5, "leftUsd": 1.5, "kind": "agent", "seat": 4,
             "session": 21, "bellAt": "2099-10-17T20:00:00Z"}
        with patch.object(plugin, "_get_json", return_value=m) as get:
            snapshot = plugin.quotum.fetch_account_usage(api_key="actual-key")
            plugin.cap_spent_message("actual-key")
        get.assert_called_once()
        self.assertEqual(snapshot.windows[0].used_percent, 25)
        self.assertEqual(snapshot.windows[0].detail, "$1.5000 left")
        self.assertIn("agent key", snapshot.details[0])
        self.assertEqual(snapshot.windows[0].reset_at.hour, 20)
        with patch.object(plugin, "meter", return_value=None):
            self.assertTrue(plugin.quotum.fetch_account_usage(api_key="missing").unavailable_reason)

    def test_transient_errors_keep_sdk_retry_behavior(self):
        count = 0
        def respond(transport, req):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(500, json={"error": {"message": "temporary"}}, request=req)
            return httpx.Response(200, json={"id": "ok", "choices": []}, request=req)
        with patch.object(httpx.HTTPTransport, "handle_request", respond), patch.object(openai.OpenAI, "_sleep_for_retry"):
            with plugin.quotum.create_client(api_key="key", base_url=plugin.VENICE_BASE, max_retries=1) as client:
                self.assertEqual(client.chat.completions.create(model="expensive", messages=[]).id, "ok")
        self.assertEqual(count, 2)

    def test_sdk_402_once_with_meter_reset_and_actual_key(self):
        with patch.object(plugin, "_get_json", return_value={"bellAt": "2099-10-17T20:00:00Z"}) as get:
            err = self.request(402)
        self.assertIn("cap is spent", str(err))
        self.assertIn("2099-10-17 20:00 UTC", str(err))
        self.assertNotIn("raw upstream text", str(err))
        get.assert_called_once_with(plugin.SITE + "/api/meter/" + plugin.key_hash("actual-inference-key"))
        self.assertEqual(self.sent[0].headers["authorization"], "Bearer actual-inference-key")

    def test_sdk_401_once_and_recovery_link_without_meter(self):
        with patch.object(plugin, "_get_json") as get:
            err = self.request(401)
        get.assert_not_called()
        for phrase in ("rotated or ended", "seat was freed", "https://quotum.org/seat"):
            self.assertIn(phrase, str(err))

    def test_bell_fallback_before_at_and_after(self):
        with patch.object(plugin, "meter", return_value=None):
            for hour, day in [(19, 17), (20, 18), (21, 18)]:
                msg = plugin.cap_spent_message("key", now=datetime(2026, 10, 17, hour, tzinfo=timezone.utc))
                self.assertIn(f"2026-10-{day} 20:00 UTC", msg)

    def test_stale_naive_or_invalid_meter_bell_is_computed(self):
        for value in ("bad", "2020-01-01T20:00:00Z", "2099-01-01T20:00:00", None):
            with patch.object(plugin, "meter", return_value={"bellAt": value}):
                msg = plugin.cap_spent_message("key", now=datetime(2026, 12, 31, 23, tzinfo=timezone.utc))
                self.assertIn("2027-01-01 20:00 UTC", msg)

    def test_meter_cache_hash_only_failure_and_ttl(self):
        with patch.object(plugin.time, "monotonic", return_value=100), patch.object(plugin, "_get_json", side_effect=OSError("offline")) as get:
            self.assertIsNone(plugin.meter("secret"))
            self.assertIsNone(plugin.meter("secret"))
            self.assertEqual(get.call_count, 1)
        with patch.object(plugin.time, "monotonic", return_value=400), patch.object(plugin, "_get_json", return_value={"leftUsd": 1}) as get:
            self.assertEqual(plugin.meter("secret")["leftUsd"], 1)
            self.assertEqual(get.call_count, 1)
            self.assertNotIn("secret", get.call_args.args[0])

    def test_cache_interval_validation(self):
        for value, expected in [("0", 60), ("-2", 60), ("bad", 300), ("nan", 300), ("inf", 300), ("10", 600)]:
            with patch.dict(os.environ, {"QUOTUM_PREFLIGHT_MINUTES": value}):
                self.assertEqual(plugin._cache_seconds(), expected)

    def test_preflight_once_per_session_cheaper_private_no_switch(self):
        def get(url, **kwargs):
            return {"models": ROWS} if url.endswith("/models") else {"leftUsd": 0.01}
        with patch.dict(os.environ, {"QUOTUM_PREFLIGHT": "1"}), patch.object(plugin, "_get_json", side_effect=get) as fetch:
            with self.assertLogs(plugin.logger, level="WARNING") as logs:
                plugin.preflight("key", "expensive", "session-1")
                plugin.preflight("key", "expensive", "session-1")
                plugin.preflight("rotated-key", "expensive", "session-1")
                plugin.preflight("key", "expensive", "session-2")
            self.assertEqual(len(logs.output), 2)
            self.assertIn("private-small", logs.output[0])
            self.assertIn("model unchanged", logs.output[0])
            self.assertEqual(fetch.call_count, 2)  # one public catalog, one hash meter
            self.assertNotIn("private prompt", str(fetch.call_args_list))

    def test_preflight_disabled_missing_session_sufficient_unknown_and_failures(self):
        with patch.object(plugin, "_get_json") as get:
            plugin.preflight("key", "expensive", "s")
            get.assert_not_called()
        with patch.dict(os.environ, {"QUOTUM_PREFLIGHT": "1"}), patch.object(plugin, "catalog", return_value=ROWS), patch.object(plugin, "meter", return_value={"leftUsd": 1}), patch.object(plugin.logger, "warning") as warn:
            plugin.preflight("key", "expensive", None)
            plugin.preflight("key", "missing", "s")
            plugin.preflight("key", "expensive", "s")
            warn.assert_not_called()
        with patch.dict(os.environ, {"QUOTUM_PREFLIGHT": "1"}), patch.object(plugin, "_get_json", side_effect=OSError), patch.object(plugin.logger, "warning") as warn:
            plugin.preflight("key", "expensive", "s")
            plugin.preflight("key", "expensive", "s")
            warn.assert_not_called()

    def test_invalid_prices_and_no_cheaper_private_do_not_warn(self):
        for value in (None, "1", True, float("nan"), float("inf"), -1):
            self.assertIsNone(plugin._turn_cost({"in": value, "out": 1}))
        rows = [{"id": "expensive", "privacy": "private", "in": 1, "out": 1}]
        with patch.dict(os.environ, {"QUOTUM_PREFLIGHT": "1"}), patch.object(plugin, "catalog", return_value=rows), patch.object(plugin, "meter") as meter:
            plugin.preflight("key", "expensive", "s")
            meter.assert_not_called()

    def test_session_hook_and_x_search_regression(self):
        with patch.dict(os.environ, {"QUOTUM_X_SEARCH": "1"}), patch.object(plugin, "x_search_models", return_value={"grok"}):
            body = plugin.quotum.build_extra_body(session_id="s", model="grok")
            self.assertTrue(body["venice_parameters"]["enable_x_search"])
            self.assertFalse(body["venice_parameters"]["include_venice_system_prompt"])
            self.assertEqual(plugin._SESSION.get(), "s")
            self.request(401, x_search=True)
            data = json.loads(self.sent[0].content)
            self.assertEqual([t["function"]["name"] for t in data["tools"]], ["other"])
        self.assertIsNone(plugin._without_web_search(b'{"model":"other"}'))

    def test_release_versions_match(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(json.loads((root / "dashboard/manifest.json").read_text())["version"], plugin.VERSION)
        self.assertIn(f"version: {plugin.VERSION}\n", (root / "plugin.yaml").read_text())

    def test_metadata_headers_and_redirect_block(self):
        send = MagicMock(side_effect=lambda *a, **kw: io.StringIO('{"leftUsd":1}'))
        opener = SimpleNamespace(open=send)
        with patch.object(plugin.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(plugin._get_json(plugin.SITE + "/api/meter/0123456789abcdef"), {"leftUsd": 1})
        request = send.call_args.args[0]
        self.assertEqual(request.get_header("X-quotum-plugin"), "0.3.2")
        self.assertEqual(request.get_header("User-agent"), "hermes-quotum/0.3.2")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.data)
        self.assertIsInstance(build.call_args.args[0], plugin._NoRedirect)
        with self.assertRaises(plugin.urllib.error.HTTPError):
            plugin._NoRedirect().redirect_request(SimpleNamespace(full_url=plugin.SITE), None, 302, "redirect", {}, "https://elsewhere.example")

    def test_inference_cannot_send_key_to_other_host(self):
        with patch.object(httpx.HTTPTransport, "handle_request") as send:
            with plugin.quotum.create_client(api_key="secret", base_url="https://elsewhere.example", max_retries=0) as client:
                with self.assertRaises(openai.APIConnectionError):
                    client.chat.completions.create(model="expensive", messages=[])
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
