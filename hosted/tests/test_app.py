import copy
import socket
import time
import unittest
import uuid
from contextlib import contextmanager
from unittest import mock

import requests
from urllib3.util import connection as urllib3_connection

from app import ApiError, ISSUER, Store, create_app, owner_key
from engine import Engine, MAX_INPUT_TOKENS


ORIGIN = "https://agents.a-11-oy.com"
OWNER = owner_key("synthetic-user")
CONVERSATION_ID = str(uuid.uuid4())
KEY = str(uuid.uuid4())


class FakeStore:
    def __init__(self):
        self.calls = []
        self.messages = []
        self.runs = []
        self.unavailable = set()
        self.stale = False
        self.other_messages = [{"owner": owner_key("other"), "content": "Private other data"}]

    def call(self, op, **data):
        self.calls.append((op, copy.deepcopy(data)))
        if op in self.unavailable:
            raise ApiError("store_unavailable", 503)
        if op == "check":
            return {"status": "available", "schema_version": 1, "receipt_minted": False}
        if op == "list":
            return {"conversations": [{"owner": data["owner"], "id": CONVERSATION_ID}],
                    "receipt_minted": False}
        if op == "get":
            if data["id"] != CONVERSATION_ID or data["owner"] != OWNER:
                raise ApiError("not_found", 404)
            return {"conversation": {"owner": OWNER, "id": CONVERSATION_ID},
                    "messages": copy.deepcopy(self.messages), "runs": copy.deepcopy(self.runs),
                    "receipt_minted": False}
        if op == "reserve":
            if self.stale:
                raise ApiError("context_changed", 409)
            if data["expected_message_count"] != len(self.messages):
                raise AssertionError("missing message count admission")
            run = {"id": str(uuid.uuid4()), "owner": data["owner"],
                   "conversation_id": data["conversation_id"],
                   "idempotency_key": data["idempotency_key"], "prompt": data["prompt"],
                   "max_output_tokens": data["max_output_tokens"], "state": "pending"}
            self.runs.append(run)
            self.messages.append({"role": "user", "content": data["prompt"]})
            return {"status": "pending", "run": copy.deepcopy(run)}
        if op == "complete":
            run = next(item for item in self.runs if item["id"] == data["run_id"])
            run["state"] = "completed"
            run["output"] = data["output"]
            self.messages.append({"role": "assistant", "content": data["output"]})
            return {"status": "completed", "run": copy.deepcopy(run),
                    "receipt": {"kind": "run.completed"}}
        if op == "uncertain":
            run = next(item for item in self.runs if item["id"] == data["run_id"])
            run["state"] = "uncertain"
            return {"status": "uncertain", "run": copy.deepcopy(run)}
        if op == "purge":
            if data["owner"] != OWNER:
                raise AssertionError("purge targeted the wrong owner")
            deleted = {"messages": len(self.messages), "runs": len(self.runs),
                       "receipts": 0, "conversations": 1}
            self.messages.clear()
            self.runs.clear()
            return {"status": "deleted", "deleted": deleted,
                    "receipt": {"kind": "account.data_erased"}}
        raise AssertionError(f"unexpected store op {op}")


class FakeEngine:
    def __init__(self):
        self.ready = True
        self.busy = False
        self.context_limit = False
        self.fail_generation = False
        self.admissions = []
        self.generations = 0
        self.active = 0

    def identity(self):
        return {"state": "MEASURED" if self.ready else "UNAVAILABLE"}

    @contextmanager
    def admission(self, messages):
        self.admissions.append(copy.deepcopy(messages))
        if not self.ready:
            raise RuntimeError("model_unavailable")
        if self.busy:
            raise RuntimeError("model_busy")
        if self.context_limit:
            raise ValueError("context_limit")
        self.active += 1
        try:
            yield self.generate
        finally:
            self.active -= 1

    def generate(self):
        self.generations += 1
        if self.fail_generation:
            raise RuntimeError("synthetic_generation_failure")
        return {"text": "Synthetic answer", "output_tokens": 2,
                "provider_response_id": None}


class FakeOAuth:
    def __init__(self, userinfo=None, failure=None):
        self.userinfo = userinfo
        self.failure = failure
        self.redirects = 0

    def authorize_redirect(self, callback):
        self.redirects += 1
        if callback != ORIGIN + "/auth/callback":
            raise AssertionError("wrong callback")
        from flask import redirect
        return redirect(ISSUER + "/oauth/authorize", code=302)

    def authorize_access_token(self):
        if self.failure:
            raise self.failure
        return {"userinfo": self.userinfo, "access_token": "synthetic-secret"}


class HostedAppTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.engine = FakeEngine()
        self.oauth = FakeOAuth({"iss": ISSUER, "sub": "synthetic-user",
                                "preferred_username": "Synthetic user"})
        self.app = create_app({"TESTING": True, "PUBLIC_ORIGIN": ORIGIN,
                               "SESSION_SECRET": "s" * 32, "OAUTH_CLIENT_ID": "synthetic-id",
                               "OAUTH_CLIENT_SECRET": "synthetic-secret",
                               "OPENID_PROVIDER_URL": ISSUER},
                              store=self.store, engine=self.engine, oauth_client=self.oauth)
        self.client = self.app.test_client()
        self.base = {"base_url": ORIGIN}

    def sign_in(self, subject="synthetic-user"):
        with self.client.session_transaction(base_url=ORIGIN) as browser:
            browser["csrf"] = "synthetic-csrf"
            browser["user"] = {"owner": owner_key(subject), "name": "Synthetic user",
                               "authenticated_at": time.time()}

    def post(self, path, payload, origin=ORIGIN, csrf="synthetic-csrf"):
        return self.client.post(path, json=payload,
                                headers={"Origin": origin, "X-CSRF-Token": csrf}, **self.base)

    def run_request(self, key=KEY, prompt="A synthetic question"):
        return self.post("/api/runs", {"conversation_id": CONVERSATION_ID,
                                      "prompt": prompt, "idempotency_key": key})

    def test_oauth_callback_checks_issuer_and_does_not_persist_token(self):
        self.oauth.userinfo["iss"] = "https://other.example"
        response = self.client.get("/auth/callback", **self.base)
        self.assertEqual(response.status_code, 401)
        with self.client.session_transaction(base_url=ORIGIN) as browser:
            self.assertNotIn("user", browser)
        self.oauth.userinfo["iss"] = ISSUER
        response = self.client.get("/auth/callback", **self.base)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], ORIGIN + "/")
        with self.client.session_transaction(base_url=ORIGIN) as browser:
            self.assertEqual(browser["user"]["owner"], OWNER)
            self.assertNotIn("access_token", browser)
            self.assertNotIn("synthetic-secret", str(dict(browser)))

    def test_origin_and_csrf_block_mutations(self):
        self.sign_in()
        payload = {"title": "Synthetic conversation"}
        for origin, csrf in [("https://other.example", "synthetic-csrf"),
                             (ORIGIN, "wrong")]:
            response = self.post("/api/conversations", payload, origin=origin, csrf=csrf)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.calls, [])
        response = self.client.post("/auth/login", data={"csrf": "synthetic-csrf"},
                                    headers={"Origin": "https://other.example"}, **self.base)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.oauth.redirects, 0)

    def test_cross_owner_conversation_is_not_readable(self):
        self.sign_in("different-user")
        response = self.client.get("/api/conversations/" + CONVERSATION_ID, **self.base)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.store.calls[-1][1]["owner"], owner_key("different-user"))

    def test_store_failure_is_reported_before_model_readiness(self):
        self.sign_in()
        self.store.unavailable.add("get")
        self.engine.ready = False
        response = self.run_request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "store_unavailable")
        self.assertEqual(self.engine.admissions, [])

    def test_busy_and_context_limit_do_not_reserve(self):
        self.sign_in()
        for flag, expected_status, expected_error in [
            ("busy", 429, "model_busy"), ("context_limit", 400, "context_limit")
        ]:
            setattr(self.engine, flag, True)
            response = self.run_request()
            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(response.json["error"], expected_error)
            self.assertEqual([op for op, _ in self.store.calls if op == "reserve"], [])
            self.assertEqual(self.engine.active, 0)
            setattr(self.engine, flag, False)

    def test_stale_context_releases_admission_without_dispatch(self):
        self.sign_in()
        self.store.messages = [{"role": "user", "content": "Previous question"},
                               {"role": "assistant", "content": "Previous answer"}]
        self.store.stale = True
        response = self.run_request()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"], "context_changed")
        self.assertEqual(self.store.calls[-1][1]["expected_message_count"], 2)
        self.assertEqual(self.engine.active, 0)
        self.assertEqual(self.engine.generations, 0)

    def test_replay_never_redispatches_even_when_model_busy(self):
        self.sign_in()
        first = self.run_request()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["status"], "completed")
        self.assertEqual(self.engine.generations, 1)
        self.engine.busy = True
        repeated = self.run_request()
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.json["replay"])
        self.assertEqual(repeated.json["status"], "completed")
        self.assertEqual(self.engine.generations, 1)
        self.assertEqual([op for op, _ in self.store.calls].count("reserve"), 1)
        conflict = self.run_request(prompt="Changed prompt")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["error"], "idempotency_conflict")
        self.assertEqual(self.engine.active, 0)

    def test_unresolved_generation_retains_reservation_without_retry(self):
        self.sign_in()
        self.engine.fail_generation = True
        first = self.run_request()
        self.assertEqual(first.status_code, 503)
        self.assertEqual(first.json["status"], "uncertain")
        self.assertEqual(self.store.runs[0]["state"], "uncertain")
        self.assertEqual(self.engine.active, 0)
        self.engine.fail_generation = False
        repeated = self.run_request()
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json["status"], "uncertain")
        self.assertTrue(repeated.json["replay"])
        self.assertEqual(self.engine.generations, 1)
        self.assertEqual([op for op, _ in self.store.calls].count("reserve"), 1)

    def test_mismatched_replay_owner_fails_closed(self):
        self.sign_in()
        self.store.runs = [{"id": str(uuid.uuid4()), "owner": owner_key("other"),
                            "conversation_id": CONVERSATION_ID, "idempotency_key": KEY,
                            "prompt": "A synthetic question", "max_output_tokens": 128,
                            "state": "completed"}]
        response = self.run_request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "store_unavailable")
        self.assertEqual(self.engine.generations, 0)

    def test_get_routes_do_not_write_store(self):
        self.sign_in()
        for path in ["/healthz", "/readyz", "/api/build-info", "/api/session",
                     "/api/conversations", "/api/conversations/" + CONVERSATION_ID]:
            self.assertIn(self.client.get(path, **self.base).status_code, {200, 503})
        self.assertTrue(all(op in {"check", "list", "get"} for op, _ in self.store.calls))

    def test_account_deletion_requires_auth_origin_and_csrf(self):
        for headers in [{"Origin": ORIGIN, "X-CSRF-Token": "synthetic-csrf"}]:
            unauthenticated = self.client.delete("/api/account", json={}, headers=headers, **self.base)
            self.assertEqual(unauthenticated.status_code, 401)
        self.sign_in()
        for origin, csrf in [("https://other.example", "synthetic-csrf"),
                             (ORIGIN, "wrong")]:
            denied = self.client.delete("/api/account", json={},
                                        headers={"Origin": origin, "X-CSRF-Token": csrf}, **self.base)
            self.assertEqual(denied.status_code, 403)
        self.assertFalse(any(op == "purge" for op, _ in self.store.calls))
        self.assertEqual(len(self.store.other_messages), 1)

    def test_account_deletion_erases_owner_and_signs_out_after_success(self):
        self.sign_in()
        self.store.messages = [{"role": "user", "content": "Private owner data"}]
        response = self.client.delete("/api/account", json={},
                                      headers={"Origin": ORIGIN, "X-CSRF-Token": "synthetic-csrf"},
                                      **self.base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "deleted")
        self.assertEqual(response.json["receipt"]["kind"], "account.data_erased")
        self.assertEqual(self.store.messages, [])
        self.assertEqual(len(self.store.other_messages), 1)
        self.assertEqual(self.store.calls[-1], ("purge", {"owner": OWNER}))
        self.assertEqual(self.client.get("/api/conversations", **self.base).status_code, 401)

    def test_account_deletion_failure_preserves_session(self):
        self.sign_in()
        self.store.unavailable.add("purge")
        response = self.client.delete("/api/account", json={},
                                      headers={"Origin": ORIGIN, "X-CSRF-Token": "synthetic-csrf"},
                                      **self.base)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.client.get("/api/conversations", **self.base).status_code, 200)


class EngineAdmissionTests(unittest.TestCase):
    def test_token_limit_preflight_releases_actual_lock(self):
        class Tokenizer:
            def __init__(self):
                self.token_count = MAX_INPUT_TOKENS + 1

            def apply_chat_template(self, messages, **kwargs):
                return "synthetic prompt"

            def __call__(self, prompt, **kwargs):
                class InputIds:
                    shape = (1, self.token_count)

                return {"input_ids": InputIds()}

        engine = Engine(start=False)
        engine.ready = True
        engine._tokenizer = Tokenizer()
        with self.assertRaisesRegex(ValueError, "context_limit"):
            with engine.admission([{"role": "user", "content": "hello"}]):
                self.fail("oversized input admitted")
        self.assertFalse(engine._lock.locked())
        engine._tokenizer.token_count = 2
        with engine.admission([{"role": "user", "content": "hello"}]):
            self.assertTrue(engine._lock.locked())
        self.assertFalse(engine._lock.locked())


class StoreTransportTests(unittest.TestCase):
    def _response(self):
        response = mock.Mock()
        response.status_code = 200
        response.headers = {"content-type": "application/json"}
        response.iter_content = lambda chunk_size: [
            b'{"status":"available","schema_version":1,"receipt_minted":false}'
        ]
        response.__enter__ = lambda self: self
        response.__exit__ = lambda *args: None
        return response

    def test_store_call_uses_ipv6_and_restores_resolver(self):
        seen = []
        original = urllib3_connection.allowed_gai_family

        class Capture(requests.Session):
            def post(self, url, **kwargs):
                seen.append(urllib3_connection.allowed_gai_family())
                return StoreTransportTests._response(None)

        store = Store("https://example.account.workers.dev", "a" * 32)
        with mock.patch("app.requests.Session", Capture):
            body = store.call("check")
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(seen, [socket.AF_INET6])
        self.assertIs(urllib3_connection.allowed_gai_family, original)

    def test_store_call_falls_back_to_ipv4_after_transport_failure(self):
        seen = []
        original = urllib3_connection.allowed_gai_family

        class Capture(requests.Session):
            def post(self, url, **kwargs):
                seen.append(urllib3_connection.allowed_gai_family())
                if len(seen) == 1:
                    raise requests.ConnectionError("synthetic")
                return StoreTransportTests._response(None)

        store = Store("https://example.account.workers.dev", "a" * 32)
        with mock.patch("app.requests.Session", Capture):
            body = store.call("check")
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(seen, [socket.AF_INET6, socket.AF_INET])
        self.assertIs(urllib3_connection.allowed_gai_family, original)


if __name__ == "__main__":
    unittest.main()
