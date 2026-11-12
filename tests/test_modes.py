"""Offline contract tests: Hermes hook fixtures, real OpenAI SDK and FastAPI routes."""

import dataclasses
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

import httpx
import openai
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
HERMES_SOURCE = os.environ.get("HERMES_SOURCE")
SEAT_KEY = "fixture-seat-key"
PAYG_KEY = "fixture-project-key"
METER = {"project": "project-fixture", "total": 2_000_000_000, "available": 1_750_000_000,
         "held": 200_000_000, "refundPending": 50_000_000, "spent": 1_000_000_000,
         "frozen": False, "unit": "USD-nano", "receipts": [
             {"id": "request-finished", "status": "charged", "model": "openai-gpt-6-luna",
              "charge": 37_500, "refund": 0, "footer": {"finalPrice": 37_500, "burnShare": 3825, "tokensIn": 20, "tokensOut": 10}},
             {"id": "request-pending", "status": "pending-reconcile", "model": "openai-gpt-6-luna",
              "charge": 0, "refund": 0, "footer": {"finalPrice": None, "burnShare": None}},
         ]}


def load(name, path, package=False):
    spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(ROOT)] if package else None)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def hook_fixtures():
    # Only the small registration/usage contract is mocked; HTTP and SDK behavior are real.
    providers = types.ModuleType("providers")
    providers.register_provider = lambda profile: None
    base = types.ModuleType("providers.base")
    fields = ["name", "display_name", "description", "signup_url", "env_vars", "base_url", "auth_type", "api_mode", "hostname", "classify_api_error"]
    base.ProviderProfile = dataclasses.make_dataclass("ProviderProfile", [(f, object, dataclasses.field(default=None)) for f in fields])
    base.ProviderProfile.get_max_tokens = lambda self, model: None
    if HERMES_SOURCE:
        base = load("providers.base", Path(HERMES_SOURCE) / "providers/base.py")
    usage = types.ModuleType("agent.account_usage")
    usage.AccountUsageWindow = dataclasses.make_dataclass("AccountUsageWindow", [(f, object, dataclasses.field(default=None)) for f in ("label", "used_percent", "reset_at", "detail")])
    usage.AccountUsageSnapshot = dataclasses.make_dataclass("AccountUsageSnapshot", [(f, object, dataclasses.field(default=None)) for f in ("provider", "source", "fetched_at", "title", "unavailable_reason", "windows", "details", "raw")])
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name: os.environ.get(name)
    return {"agent.secret_scope": scope, "providers": providers, "providers.base": base, "agent": types.ModuleType("agent"), "agent.account_usage": usage}


class ModesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: self.tmp.name
        self.start(patch.dict(sys.modules, {**hook_fixtures(), "hermes_constants": constants}))
        self.start(patch.dict(os.environ, {"QUOTUM_SEAT_KEY": SEAT_KEY}, clear=True))
        self.start(patch("urllib.request.urlopen", side_effect=AssertionError("unmocked network access")))
        self.start(patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("unmocked network access")))
        self.plugin = load("quotum_test_plugin", ROOT / "__init__.py", True)
        self.api = load("quotum_test_dashboard", ROOT / "dashboard/plugin_api.py")

    def start(self, p):
        value = p.start()
        self.addCleanup(p.stop)
        return value

    def payg(self):
        os.environ["QUOTUM_PAYG_KEY"] = "  " + PAYG_KEY + "  "

    def json_routes(self, routes):
        self.seen = []

        def urlopen(request, **kwargs):
            self.seen.append(request)
            result = routes[request.full_url]
            if isinstance(result, Exception):
                raise result
            return io.BytesIO(json.dumps(result).encode())

        self.start(patch("urllib.request.build_opener", return_value=SimpleNamespace(open=urlopen)))

    def sdk_client(self, handler):
        self.start(patch("httpx.HTTPTransport.handle_request", side_effect=httpx.MockTransport(handler).handle_request))
        client = self.plugin.quotum.create_client(api_key=SEAT_KEY, base_url=self.plugin.VENICE_BASE, max_retries=9, future_unknown_field=True)
        self.addCleanup(client.close)
        return client

    def test_seat_default_registration_and_hooks(self):
        q = self.plugin.quotum
        self.assertEqual(q.base_url, self.plugin.VENICE_BASE)
        self.assertEqual(q.display_name, "quotum seat")
        self.assertEqual(q.env_vars, ("QUOTUM_PAYG_KEY", "QUOTUM_SEAT_KEY"))
        q.create_client(api_key=SEAT_KEY).close()
        self.assertEqual(q.build_extra_body(), {"venice_parameters": {"include_venice_system_prompt": False}})
        self.assertIsNone(q.get_max_tokens("kimi-k3"))
        self.assertEqual(self.plugin.classify_error(status_code=402), {"reason": "billing", "retryable": False, "should_rotate_credential": False, "should_fallback": False})
        self.assertEqual(self.plugin.classify_error(status_code=401), {"reason": "auth_permanent", "retryable": False, "should_rotate_credential": False, "should_fallback": False})
        self.assertIsNone(self.plugin.classify_error(status_code=403))

    def test_blank_payg_key_keeps_seat_mode(self):
        os.environ["QUOTUM_PAYG_KEY"] = "  "
        self.plugin.quotum.create_client(api_key=SEAT_KEY).close()

    def test_seat_x_search_hooks_are_preserved(self):
        os.environ["QUOTUM_X_SEARCH"] = "1"
        self.plugin._X_MODELS = {"grok-4-7"}
        body = self.plugin.quotum.build_extra_body(model="grok-4-7")
        self.assertTrue(body["venice_parameters"]["enable_x_search"])
        request = {**body, "tools": [{"function": {"name": "web_search"}}], "tool_choice": "auto"}
        filtered = json.loads(self.plugin._without_web_search(json.dumps(request).encode()))
        self.assertNotIn("tools", filtered)
        self.assertNotIn("tool_choice", filtered)
        sentinel = object()
        custom = self.start(patch.object(self.plugin, "_quotum_client", return_value=sentinel))
        self.assertIs(self.plugin.quotum.create_client(api_key=SEAT_KEY), sentinel)
        custom.assert_called_once()

    def test_payg_registration_and_precedence(self):
        self.payg()
        plugin = load("quotum_payg_registration", ROOT / "__init__.py", True)
        self.assertEqual(plugin.quotum.base_url, "https://quotum.org/api/payg/v1")
        self.assertEqual(plugin.quotum.hostname, "quotum.org")
        self.assertEqual(plugin.quotum.display_name, "quotum pay as you go")

    def test_seat_chat_wire(self):
        seen = []

        def handler(req):
            seen.append(req)
            return httpx.Response(200, json={"id": "seat-reply", "choices": [{"index": 0, "message": {"role": "assistant", "content": "seat"}, "finish_reason": "stop"}]})

        client = self.sdk_client(handler)
        reply = client.chat.completions.create(model="kimi-k3", messages=[{"role": "user", "content": "hello"}], extra_body=self.plugin.quotum.build_extra_body())
        self.assertEqual(reply.choices[0].message.content, "seat")
        self.assertEqual(str(seen[0].url), "https://api.venice.ai/api/v1/chat/completions")
        self.assertEqual(seen[0].headers["authorization"], "Bearer " + SEAT_KEY)
        self.assertFalse(seen[0].headers.get("idempotency-key"))

    def test_payg_chat_wire_json_and_unique_admission(self):
        self.payg()
        os.environ["QUOTUM_X_SEARCH"] = "1"
        seen = []

        def handler(req):
            seen.append(req)
            return httpx.Response(200, json={"id": "payg-reply", "choices": [{"index": 0, "message": {"role": "assistant", "content": "payg"}, "finish_reason": "stop"}], "quotum_billing": METER["receipts"][0]})

        client = self.sdk_client(handler)
        self.assertEqual(client.max_retries, 0)
        self.assertFalse(client._client.follow_redirects)
        for _ in range(2):
            reply = client.chat.completions.create(model="openai-gpt-6-luna", max_tokens=self.plugin.quotum.get_max_tokens(None), messages=[{"role": "user", "content": "hello"}], extra_body=self.plugin.quotum.build_extra_body(model="grok-4-7"))
            self.assertEqual(reply.quotum_billing["id"], "request-finished")
        for req in seen:
            self.assertEqual(str(req.url), "https://quotum.org/api/payg/v1/chat/completions")
            self.assertEqual(req.headers["authorization"], "Bearer " + PAYG_KEY)
            self.assertGreaterEqual(len(req.headers["idempotency-key"]), 8)
            self.assertNotIn("venice_parameters", json.loads(req.content))
        self.assertNotEqual(seen[0].headers["idempotency-key"], seen[1].headers["idempotency-key"])

    def test_payg_stream_preserves_receipt(self):
        self.payg()
        frames = [{"id": "stream", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": "hello"}}]},
                  {"id": "stream", "object": "chat.completion.chunk", "choices": [], "quotum_billing": METER["receipts"][0]}]
        wire = "".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n"
        client = self.sdk_client(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire))
        stream = list(client.chat.completions.create(model="openai-gpt-6-luna", max_tokens=100, messages=[{"role": "user", "content": "hello"}], stream=True))
        self.assertEqual(stream[0].choices[0].delta.content, "hello")
        self.assertEqual(stream[-1].quotum_billing["footer"]["finalPrice"], 37_500)

    def test_payg_chat_errors_one_line_no_retries_or_key_echo(self):
        self.payg()
        cases = [(403, "invite-required", "check project access"), (402, "insufficient_credit", "top up"),
                 (503, "reconciliation-required", "wait for intake"), (503, "pilot-loss-limit", "wait for intake"),
                 (401, "invalid-key", "create a new"), (403, "key-model-scope", "choose a model")]
        for status, code, action in cases:
            with self.subTest(code=code):
                seen = []

                def handler(req):
                    seen.append(req)
                    return httpx.Response(status, json={"error": {"code": code, "message": PAYG_KEY}})

                client = self.sdk_client(handler)
                with self.assertRaises(openai.APIStatusError) as caught:
                    client.chat.completions.create(model="openai-gpt-6-luna", messages=[{"role": "user", "content": "hello"}], max_tokens=100)
                msg = caught.exception.body["message"]
                self.assertIn(action, msg)
                self.assertNotIn("\n", msg)
                self.assertNotIn("credit", msg.lower())
                self.assertNotIn(PAYG_KEY, str(caught.exception))
                self.assertNotIn("credit", str(caught.exception).lower())
                self.assertEqual(len(seen), 1)
                verdict = self.plugin.classify_error(status_code=status, body={"error": {"code": code}})
                self.assertFalse(verdict["retryable"])

    def test_payg_explicit_idempotency_is_preserved(self):
        self.payg()
        seen = []

        def handler(req):
            seen.append(req)
            return httpx.Response(200, json={"id": "response", "choices": []})

        self.sdk_client(handler).chat.completions.create(model="openai-gpt-6-luna", messages=[{"role": "user", "content": "hello"}], extra_headers={"Idempotency-Key": "fixture-existing"})
        self.assertEqual(seen[0].headers["idempotency-key"], "fixture-existing")

    def test_seat_catalog_and_meter_do_not_send_bearer(self):
        p = self.plugin
        meter_url = p.SITE + "/api/meter/" + p.key_hash(SEAT_KEY)
        self.json_routes({p.SITE + "/api/seat/models": {"models": [{"id": "kimi-k3", "privacy": "private"}, {"id": "excluded", "privacy": "public"}]},
                          meter_url: {"capUsd": 2, "spentUsd": 0.5, "leftUsd": 1.5, "seat": 3, "kind": "seat", "session": 5, "bellAt": "2026-10-11T20:00:00Z"}})
        self.assertEqual(p.quotum.fetch_models(), ["kimi-k3"])
        snapshot = p.quotum.fetch_account_usage()
        self.assertEqual(snapshot.windows[0].used_percent, 25)
        self.assertEqual(snapshot.windows[0].reset_at.hour, 20)
        self.assertEqual(snapshot.source, "quotum_meter")
        for req in self.seen:
            self.assertIsNone(req.get_header("Authorization"))
            self.assertNotIn(SEAT_KEY, req.full_url)

    def test_payg_catalog_only_text_chat(self):
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + "/models": {"data": [
            {"id": "openai-gpt-6-luna", "privacy": "anonymized", "type": "text", "endpoints": ["/chat/completions"]},
            {"id": "image", "privacy": "private", "type": "image", "endpoints": ["/images/generations"]}]}})
        self.assertEqual(self.plugin.quotum.fetch_models(), ["openai-gpt-6-luna"])
        self.assertIsNone(self.seen[0].get_header("Authorization"))

    def test_payg_usage_balance_units_and_pending_receipts(self):
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + "/meter": METER, self.plugin.PAYG_BASE + "/status": {"intake": False}})
        snapshot = self.plugin.quotum.fetch_account_usage(api_key=SEAT_KEY)
        self.assertEqual(snapshot.source, "quotum_payg")
        self.assertEqual(snapshot.raw["balance"]["availableUsd"], 1.75)
        self.assertEqual(snapshot.windows[0].label, "balance")
        self.assertIsNone(snapshot.windows[0].reset_at)
        self.assertIsNone(snapshot.raw["receipts"][0]["chargeUsd"])
        self.assertAlmostEqual(snapshot.raw["receipts"][1]["chargeUsd"], .0000375)
        self.assertIn("paused", " ".join(snapshot.details))
        self.assertNotIn("credit", json.dumps(dataclasses.asdict(snapshot), default=str).lower())
        self.assertEqual(self.seen[0].get_header("Authorization"), "Bearer " + PAYG_KEY)
        self.assertIsNone(self.seen[1].get_header("Authorization"))

    def test_dashboard_payg_is_fresh_and_never_exposes_bearer(self):
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + "/meter": METER, self.plugin.PAYG_BASE + "/status": {"intake": True}})
        app = FastAPI()
        app.include_router(self.api.router, prefix="/api/plugins/quotum")
        with TestClient(app) as client:
            result = client.get("/api/plugins/quotum/account").json()
            client.get("/api/plugins/quotum/record")
            client.get("/api/plugins/quotum/payg")
        self.assertEqual(result["mode"], "payg")
        self.assertEqual(result["balance"]["availableUsd"], 1.75)
        self.assertEqual(len(self.seen), 6)
        self.assertNotIn(PAYG_KEY, json.dumps(result))
        self.assertNotIn(SEAT_KEY, json.dumps(result))
        self.assertNotIn("credit", json.dumps(result).lower())

    def test_dashboard_seat_record_cache_preserved(self):
        meter_url = self.plugin.SITE + "/api/meter/" + self.plugin.key_hash(SEAT_KEY)
        self.json_routes({meter_url: {"seat": 3}, self.plugin.SITE + "/api/record?seat=3": {"record": {"seals": {}, "counts": {}}, "wallet": "fixture-wallet"}})
        self.assertEqual(self.api.account()["seat"], 3)
        self.api.record()
        self.assertEqual(len(self.seen), 2)
        self.assertTrue(all(req.get_header("Authorization") is None for req in self.seen))

    def test_dashboard_profile_scope_and_payg_precedence_over_wallet(self):
        # Hermes supplies the selected profile's secrets, rather than the backend reading files.
        self.start(patch("agent.secret_scope.get_secret", side_effect=lambda name: {"QUOTUM_PAYG_KEY": PAYG_KEY, "QUOTUM_WALLET": "fixture-wallet"}.get(name, "")))
        self.json_routes({self.plugin.PAYG_BASE + "/meter": METER, self.plugin.PAYG_BASE + "/status": {"intake": True}})
        self.assertEqual(self.api.account()["mode"], "payg")
        self.assertEqual(self.seen[0].get_header("Authorization"), "Bearer " + PAYG_KEY)

    def test_dashboard_payg_errors_and_balance_remain_during_pause(self):
        self.payg()
        for status, code, expected in [(403, "invite-required", "invite"), (402, "insufficient_credit", "balance"), (503, "payg-halted", "paused")]:
            with self.subTest(code=code):
                error = urllib.error.HTTPError(self.plugin.PAYG_BASE + "/meter", status, "fixture", {}, io.BytesIO(json.dumps({"error": {"code": code}}).encode()))
                self.json_routes({self.plugin.PAYG_BASE + "/meter": error})
                result = self.api.account()
                self.assertIn(expected, result["error"])
                self.assertNotIn("credit", result["error"])
        self.json_routes({self.plugin.PAYG_BASE + "/meter": METER, self.plugin.PAYG_BASE + "/status": {"intake": False}})
        result = self.api.account()
        self.assertEqual(result["balance"]["availableUsd"], 1.75)
        self.assertIn("paused", result["notice"])

    def test_unknown_unit_is_not_misreported(self):
        self.payg()
        self.json_routes({self.plugin.PAYG_BASE + "/meter": {**METER, "unit": "other"}, self.plugin.PAYG_BASE + "/status": {"intake": True}})
        self.assertIn("unsupported balance unit", self.api.account()["error"])
        self.assertIn("unsupported balance unit", self.plugin.quotum.fetch_account_usage().unavailable_reason)

    def test_status_failure_preserves_balance_and_receipts(self):
        self.payg()
        error = urllib.error.HTTPError(self.plugin.PAYG_BASE + "/status", 503, "fixture", {}, io.BytesIO(b'{}'))
        self.json_routes({self.plugin.PAYG_BASE + "/meter": METER, self.plugin.PAYG_BASE + "/status": error})
        result = self.api.account()
        self.assertEqual(result["balance"]["availableUsd"], 1.75)
        self.assertIn("paused", result["notice"])
        snapshot = self.plugin.quotum.fetch_account_usage()
        self.assertIsNone(snapshot.unavailable_reason)
        self.assertEqual(len(snapshot.raw["receipts"]), 2)
        self.assertIn("status is unavailable", " ".join(snapshot.details))

    def test_dashboard_site_override_does_not_redirect_payg_key(self):
        self.payg()
        self.api.SITE = "https://fixture-seat-site.invalid"
        self.json_routes({self.plugin.PAYG_BASE + "/meter": METER, self.plugin.PAYG_BASE + "/status": {"intake": True}})
        self.assertEqual(self.api.account()["mode"], "payg")
        self.assertTrue(all(req.full_url.startswith(self.plugin.PAYG_BASE) for req in self.seen))
        self.assertNotIn("Authorization", self.seen[0].headers)
        self.assertEqual(self.seen[0].get_header("Authorization"), "Bearer " + PAYG_KEY)


if __name__ == "__main__":
    unittest.main()
