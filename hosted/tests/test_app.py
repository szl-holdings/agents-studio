import copy
import io
import socket
import ssl
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from unittest import mock

from urllib3.util import connection as urllib3_connection

from app import (ApiError, DiagnosticBudget, HANDSHAKE_WINDOW_BYTES, ISSUER, RESPONSE_WINDOW_BYTES, Store,
                 StoreFailure, TCP_SEGMENT_CEILING, create_app, decode_store_response,
                 model_ready, owner_key, valid_store_url)
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
        self.lose_complete_response = False
        self.fail_get_after = None
        self.substitute_model_binding = False
        self._gets = 0
        self.other_messages = [{"owner": owner_key("other"), "content": "Private other data"}]

    def call(self, op, **data):
        self.calls.append((op, copy.deepcopy(data)))
        if op in self.unavailable:
            raise ApiError("store_unavailable", 503)
        if op == "check":
            return {"status": "available", "schema_version": 1,
                    "admission_epoch_version": 1, "model_binding_enforced": True,
                    "receipt_minted": False}
        if op == "list":
            return {"conversations": [{"owner": data["owner"], "id": CONVERSATION_ID}],
                    "receipt_minted": False}
        if op == "get":
            self._gets += 1
            if self.fail_get_after is not None and self._gets > self.fail_get_after:
                raise ApiError("store_unavailable", 503)
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
                   "max_output_tokens": data["max_output_tokens"], "state": "pending",
                   "model_id": data["model_binding"]["id"],
                   "model_revision": data["model_binding"]["revision"],
                   "model_weights_sha256": data["model_binding"]["weights_sha256"],
                   "model_binding_evidence": "DECLARED"}
            self.runs.append(run)
            self.messages.append({"role": "user", "content": data["prompt"]})
            returned = copy.deepcopy(run)
            if self.substitute_model_binding:
                returned["model_weights_sha256"] = "0" * 64
            return {"status": "pending", "run": returned}
        if op == "complete":
            run = next(item for item in self.runs if item["id"] == data["run_id"])
            run["state"] = "completed"
            run["output"] = data["output"]
            self.messages.append({"role": "assistant", "content": data["output"]})
            if self.lose_complete_response:
                raise ApiError("store_unavailable", 503)
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
        self.weights_verified = True
        self.model_id = "synthetic/model"
        self.model_revision = "a" * 40
        self.model_weights_sha256 = "b" * 64

    def identity(self):
        return {"state": "SIMULATED" if self.ready else "UNAVAILABLE",
                "model": self.model_id, "revision": self.model_revision,
                "weights_sha256": self.model_weights_sha256,
                "weights_verified": self.weights_verified if self.ready else False}

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

    def rebuild(self, store):
        self.store = store
        self.app = create_app({"TESTING": True, "PUBLIC_ORIGIN": ORIGIN,
                               "SESSION_SECRET": "s" * 32, "OAUTH_CLIENT_ID": "synthetic-id",
                               "OAUTH_CLIENT_SECRET": "synthetic-secret",
                               "OPENID_PROVIDER_URL": ISSUER},
                              store=store, engine=self.engine, oauth_client=self.oauth)
        self.client = self.app.test_client()

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

    def test_hub_embedding_has_exact_frame_ancestor(self):
        for path in ("/", "/api/session", "/static/privacy.html"):
            response = self.client.get(path, **self.base)
            try:
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("X-Frame-Options", response.headers)
                policy = response.headers["Content-Security-Policy"]
                frame_directives = [directive.strip() for directive in policy.split(";")
                                    if directive.strip().startswith("frame-ancestors")]
                self.assertEqual(frame_directives, ["frame-ancestors https://huggingface.co"])
                self.assertIn("script-src 'self'", policy)
                self.assertIn("form-action 'self' https://huggingface.co", policy)
                self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
            finally:
                response.close()

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
        for origin in ("https://other.example", "null", None):
            headers = {"Origin": origin} if origin is not None else {}
            response = self.client.post("/auth/login", data={"csrf": "synthetic-csrf"},
                                        headers=headers, **self.base)
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.oauth.redirects, 0)

    def test_first_party_form_login_keeps_origin_and_csrf_gate(self):
        with self.client.session_transaction(base_url=ORIGIN) as browser:
            browser["csrf"] = "synthetic-csrf"
        response = self.client.post("/auth/login", data={"csrf": "synthetic-csrf"},
                                    headers={"Origin": ORIGIN}, **self.base)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], ISSUER + "/oauth/authorize")
        self.assertEqual(self.oauth.redirects, 1)

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

    def test_store_reservation_failure_releases_admission_without_dispatch(self):
        self.sign_in()
        self.store.unavailable.add("reserve")
        original_call = self.store.call

        def observed_call(op, **data):
            if op == "reserve":
                self.assertEqual(self.engine.active, 1)
                self.assertEqual(len(self.engine.admissions), 1)
            return original_call(op, **data)

        with mock.patch.object(self.store, "call", side_effect=observed_call):
            response = self.run_request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "store_unavailable")
        self.assertEqual([op for op, _ in self.store.calls].count("reserve"), 1)
        self.assertEqual(len(self.engine.admissions), 1)
        self.assertEqual(self.engine.active, 0)
        self.assertEqual(self.engine.generations, 0)

    def test_racing_store_replay_releases_admission_without_dispatch(self):
        test = self

        class ReplayAfterRead(FakeStore):
            def call(self, op, **data):
                if op != "reserve":
                    return super().call(op, **data)
                self.calls.append((op, copy.deepcopy(data)))
                test.assertEqual(test.engine.active, 1)
                test.assertEqual(len(test.engine.admissions), 1)
                test.assertEqual(self.runs, [])
                # Model a competing request that won after our initial read.
                winner = {"id": str(uuid.uuid4()), "owner": data["owner"],
                          "conversation_id": data["conversation_id"],
                          "idempotency_key": data["idempotency_key"], "prompt": data["prompt"],
                          "max_output_tokens": data["max_output_tokens"], "state": "pending",
                          "model_id": data["model_binding"]["id"],
                          "model_revision": data["model_binding"]["revision"],
                          "model_weights_sha256": data["model_binding"]["weights_sha256"],
                          "model_binding_evidence": "DECLARED"}
                self.runs.append(winner)
                self.messages.append({"role": "user", "content": data["prompt"]})
                return {"status": "replay", "run": copy.deepcopy(winner)}

        self.rebuild(ReplayAfterRead())
        self.sign_in()
        response = self.run_request()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["replay"])
        self.assertEqual(response.json["status"], "pending")
        self.assertEqual([op for op, _ in self.store.calls].count("reserve"), 1)
        self.assertEqual(len(self.store.runs), 1)
        self.assertEqual(len(self.store.messages), 1)
        self.assertEqual(response.json["run"]["id"], self.store.runs[0]["id"])
        self.assertEqual(len(self.engine.admissions), 1)
        self.assertEqual(self.engine.active, 0)
        self.assertEqual(self.engine.generations, 0)

    def test_new_run_binds_model_before_dispatch(self):
        self.sign_in()
        response = self.run_request()
        self.assertEqual(response.status_code, 200)
        binding = next(data["model_binding"] for op, data in self.store.calls if op == "reserve")
        self.assertEqual(binding, {"id": "synthetic/model", "revision": "a" * 40,
                                   "weights_sha256": "b" * 64})
        self.assertEqual(response.json["run"]["model_id"], binding["id"])
        self.assertEqual(response.json["run"]["model_binding_evidence"], "DECLARED")
        self.assertEqual(self.engine.generations, 1)

    def test_unverified_or_malformed_model_cannot_reserve(self):
        self.sign_in()
        for field, value in [("weights_verified", False), ("model_revision", "bad"),
                             ("model_id", "https://unexpected.example/model")]:
            original = getattr(self.engine, field)
            setattr(self.engine, field, value)
            response = self.run_request()
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json["error"], "model_unavailable")
            self.assertEqual([op for op, _ in self.store.calls if op == "reserve"], [])
            self.assertEqual(self.engine.generations, 0)
            setattr(self.engine, field, original)
        with mock.patch.object(self.engine, "identity", side_effect=ValueError("synthetic failure")):
            response = self.run_request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "model_unavailable")
        self.assertEqual([op for op, _ in self.store.calls if op == "reserve"], [])

    def test_substituted_model_binding_never_dispatches(self):
        self.sign_in()
        self.store.substitute_model_binding = True
        response = self.run_request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "store_unavailable")
        self.assertEqual(self.engine.generations, 0)
        self.assertEqual(self.engine.active, 0)

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
        self.engine.model_revision = "c" * 40
        repeated_again = self.run_request()
        self.assertTrue(repeated_again.json["replay"])
        self.assertEqual(repeated_again.json["run"]["model_revision"], "a" * 40)
        self.assertEqual(self.engine.generations, 1)
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

    def test_lost_complete_response_is_read_back_without_uncertain(self):
        self.sign_in()
        self.store.lose_complete_response = True
        response = self.run_request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["status"], "completed")
        self.assertEqual(response.json["run"]["output"], "Synthetic answer")
        self.assertIsNone(response.json["receipt"])
        self.assertEqual(self.store.runs[0]["state"], "completed")
        self.assertEqual(self.engine.generations, 1)
        self.assertNotIn("uncertain", [op for op, _ in self.store.calls])

    def test_lost_complete_without_readback_does_not_send_uncertain(self):
        self.sign_in()
        self.store.lose_complete_response = True
        self.store.fail_get_after = 1
        response = self.run_request()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "run_outcome_uncertain")
        self.assertEqual(self.store.runs[0]["state"], "completed")
        self.assertEqual(self.engine.generations, 1)
        self.assertNotIn("uncertain", [op for op, _ in self.store.calls])

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

    def test_readiness_checks_are_not_cached(self):
        self.sign_in()
        self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)
        self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)
        self.assertEqual([op for op, _ in self.store.calls].count("check"), 2)
        self.assertNotIn("store_failure", self.client.get("/api/session", **self.base).json)
        self.assertEqual([op for op, _ in self.store.calls].count("check"), 3)

    def test_unverified_model_is_not_ready_and_recovers(self):
        self.sign_in()
        self.engine.weights_verified = False
        ready = self.client.get("/readyz", **self.base)
        info = self.client.get("/api/session", **self.base)
        self.assertEqual(ready.status_code, 503)
        self.assertFalse(ready.json["ready"]["model"])
        self.assertFalse(info.json["ready"]["model"])
        self.assertEqual(self.engine.generations, 0)
        self.assertNotIn("reserve", [op for op, _ in self.store.calls])

        self.engine.weights_verified = True
        self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)
        self.assertTrue(self.client.get("/api/session", **self.base).json["ready"]["model"])

    def test_scoped_artifact_identity_cannot_be_silently_reduced(self):
        self.sign_in()
        baseline = self.engine.identity()
        for scope in ({"artifact_custody": "FRESH_HASHED_CACHE_BYTES_ONLY",
                       "loaded_gpu_weights_verified": False},
                      {"artifact_custody": "unknown"},
                      {"loaded_gpu_weights_verified": False},
                      {"loaded_gpu_weights_verified": True}):
            with self.subTest(scope=scope), mock.patch.object(self.engine, "identity", return_value={**baseline, **scope}):
                ready = self.client.get("/readyz", **self.base)
                self.assertEqual(ready.status_code, 503)
                self.assertFalse(ready.json["ready"]["model"])
                self.assertFalse(self.client.get("/api/session", **self.base).json["ready"]["model"])
                self.assertEqual(self.run_request().status_code, 503)
        self.assertNotIn("reserve", [op for op, _ in self.store.calls])
        self.assertEqual(self.engine.generations, 0)
        self.assertEqual(self.engine.active, 0)
        self.assertTrue(model_ready(self.engine))

    def test_readiness_rejects_invalid_identity_and_identity_errors(self):
        self.sign_in()
        for field, value in (("model_id", "bad model"),
                             ("model_revision", "bad"),
                             ("model_weights_sha256", "bad")):
            with self.subTest(field=field):
                old = getattr(self.engine, field)
                setattr(self.engine, field, value)
                response = self.client.get("/readyz", **self.base)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json["ready"]["model"])
                setattr(self.engine, field, old)
        with mock.patch.object(self.engine, "identity", side_effect=RuntimeError("synthetic")):
            response = self.client.get("/api/session", **self.base)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json["ready"]["model"])
        self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)

    def test_anonymous_diagnostics_do_not_probe_store(self):
        self.store.unavailable.add("check")
        first = self.client.get("/api/session", **self.base)
        second = self.client.get("/api/session", **self.base)
        for response in (first, second):
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(response.json["user"])
            self.assertFalse(response.json["ready"]["store"])
            self.assertEqual(response.json["store_evidence"], "UNKNOWN")
            self.assertNotIn("store_failure", response.json)
        self.assertEqual(first.json["csrf"], second.json["csrf"])
        self.assertEqual(self.store.calls, [])
        for _ in range(2):
            ready = self.client.get("/readyz", **self.base)
            self.assertEqual(ready.status_code, 503)
            self.assertFalse(ready.json["ready"]["store"])
            self.assertEqual(ready.json["store_evidence"], "UNKNOWN")
            self.assertNotIn("store_failure", ready.json)
            self.assertFalse(ready.json["receipt_minted"])
        self.assertEqual(self.store.calls, [])

    def test_authenticated_session_checks_store_fresh_and_recovers(self):
        self.sign_in()
        first = self.client.get("/api/session", **self.base)
        self.assertTrue(first.json["ready"]["store"])
        self.assertNotIn("store_evidence", first.json)
        self.store.unavailable.add("check")
        failed = self.client.get("/api/session", **self.base)
        self.assertFalse(failed.json["ready"]["store"])
        self.assertIn("store_failure", failed.json)
        self.store.unavailable.remove("check")
        recovered = self.client.get("/api/session", **self.base)
        self.assertTrue(recovered.json["ready"]["store"])
        self.assertNotIn("store_failure", recovered.json)
        self.assertEqual([op for op, _ in self.store.calls], ["check"] * 3)

    def test_authenticated_readiness_fails_and_recovers_without_cache(self):
        self.sign_in()
        self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)
        self.store.unavailable.add("check")
        failed = self.client.get("/readyz", **self.base)
        self.assertEqual(failed.status_code, 503)
        self.assertFalse(failed.json["ready"]["store"])
        self.assertIn("store_failure", failed.json)
        self.store.unavailable.remove("check")
        recovered = self.client.get("/readyz", **self.base)
        self.assertEqual(recovered.status_code, 200)
        self.assertTrue(recovered.json["ready"]["store"])
        self.assertNotIn("store_failure", recovered.json)
        self.assertEqual([op for op, _ in self.store.calls], ["check"] * 3)

    def test_authenticated_diagnostics_are_bounded_and_recover_fresh(self):
        self.sign_in()
        clock = [100.0]
        self.app.extensions["studio_diagnostic_budget"]._clock = lambda: clock[0]
        for index in range(6):
            response = self.client.get("/readyz" if index % 2 else "/api/session", **self.base)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json["ready"]["store"])
        for path, expected_status in (("/api/session", 200), ("/readyz", 503)):
            response = self.client.get(path, **self.base)
            self.assertEqual(response.status_code, expected_status)
            self.assertFalse(response.json["ready"]["store"])
            self.assertEqual(response.json["store_evidence"], "UNKNOWN")
            self.assertTrue(response.json["store_check_deferred"])
            self.assertNotIn("store_failure", response.json)
        self.assertEqual([op for op, _ in self.store.calls], ["check"] * 6)
        clock[0] += 60
        self.store.unavailable.add("check")
        failed = self.client.get("/readyz", **self.base)
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(failed.json["store_failure"], "connect_failure")
        self.store.unavailable.remove("check")
        recovered = self.client.get("/readyz", **self.base)
        self.assertEqual(recovered.status_code, 200)
        self.assertTrue(recovered.json["ready"]["store"])
        self.assertEqual([op for op, _ in self.store.calls], ["check"] * 8)

    def test_global_diagnostic_budget_bounds_many_accounts_without_get_writes(self):
        for index in range(30):
            self.sign_in(f"synthetic-user-{index}")
            self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)
        self.sign_in("synthetic-user-30")
        blocked = self.client.get("/readyz", **self.base)
        self.assertEqual(blocked.status_code, 503)
        self.assertEqual(blocked.json["store_evidence"], "UNKNOWN")
        self.assertTrue(blocked.json["store_check_deferred"])
        self.assertEqual([op for op, _ in self.store.calls], ["check"] * 30)

    def test_diagnostic_budget_limits_inflight_probes(self):
        budget = DiagnosticBudget(clock=lambda: 100.0)
        self.assertTrue(budget.acquire("one"))
        self.assertTrue(budget.acquire("two"))
        self.assertFalse(budget.acquire("three"))
        budget.release()
        self.assertTrue(budget.acquire("three"))
        budget.release()
        budget.release()

    def test_deferred_diagnostic_does_not_bypass_run_store_admission(self):
        self.sign_in()
        for _ in range(6):
            self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)
        deferred = self.client.get("/api/session", **self.base)
        self.assertTrue(deferred.json["store_check_deferred"])
        self.assertFalse(deferred.json["ready"]["store"])
        self.store.unavailable.add("get")
        failed = self.run_request()
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(self.engine.generations, 0)
        self.store.unavailable.remove("get")
        completed = self.run_request()
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json["status"], "completed")
        self.assertEqual(self.engine.generations, 1)
        self.assertEqual([op for op, _ in self.store.calls].count("check"), 6)

    def test_readiness_asks_for_the_comparison_before_the_check(self):
        class ComparingStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.order = []

            def compare_once(self):
                self.order.append("compare")

            def call(self, op, **data):
                self.order.append(op)
                return super().call(op, **data)

        store = ComparingStore()
        self.rebuild(store)
        self.sign_in()
        self.assertEqual(self.client.get("/readyz", **self.base).status_code, 200)
        self.assertEqual(self.client.get("/api/session", **self.base).status_code, 200)
        self.assertEqual(store.order[:4], ["compare", "check", "compare", "check"])

    def test_probe_exception_does_not_block_readiness_or_leak(self):
        class ExplodingStore(FakeStore):
            def compare_once(self):
                raise RuntimeError("synthetic " + ("b" * 32))

        store = ExplodingStore()
        self.rebuild(store)
        self.sign_in()
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            response = self.client.get("/readyz", **self.base)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([op for op, _data in store.calls], ["check"])
        logs = stderr.getvalue()
        self.assertIn("result=probe_error", logs)
        self.assertIn("error=RuntimeError", logs)
        self.assertNotIn("b" * 32, logs)

    def test_schema_mismatch_stays_unready(self):
        class SchemaStore(FakeStore):
            def call(self, op, **data):
                if op == "check":
                    self.calls.append((op, copy.deepcopy(data)))
                    return {"status": "available", "schema_version": 2, "receipt_minted": False}
                return super().call(op, **data)

        self.rebuild(SchemaStore())
        self.sign_in()
        response = self.client.get("/readyz", **self.base)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ready"]["store"])
        self.assertTrue(response.json["ready"]["auth"])
        self.assertEqual(response.json["store_failure"], "schema_mismatch")
        self.assertFalse(response.json["receipt_minted"])

    def test_missing_or_boolean_admission_epoch_stays_unready(self):
        for epoch in (None, True):
            class EpochStore(FakeStore):
                def call(self, op, **data):
                    if op == "check":
                        self.calls.append((op, copy.deepcopy(data)))
                        return {"status": "available", "schema_version": 1,
                                "admission_epoch_version": epoch,
                                "receipt_minted": False}
                    return super().call(op, **data)

            self.rebuild(EpochStore())
            self.sign_in()
            response = self.client.get("/readyz", **self.base)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json["store_failure"], "schema_mismatch")

    def test_model_binding_cutover_must_be_enforced_for_readiness(self):
        for enforced in (None, False, 1):
            class PreCutoverStore(FakeStore):
                def call(self, op, **data):
                    if op == "check":
                        self.calls.append((op, copy.deepcopy(data)))
                        return {"status": "available", "schema_version": 1,
                                "admission_epoch_version": 1,
                                "model_binding_enforced": enforced,
                                "receipt_minted": False}
                    return super().call(op, **data)

            self.rebuild(PreCutoverStore())
            self.sign_in()
            response = self.client.get("/readyz", **self.base)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json["store_failure"], "schema_mismatch")

    def test_uncertain_purge_preserves_later_data_and_session(self):
        class LosingPurge(FakeStore):
            def call(self, op, **data):
                if op == "purge":
                    self.calls.append((op, copy.deepcopy(data)))
                    self.messages.clear()
                    if sum(item[0] == "purge" for item in self.calls) == 1:
                        self.messages.append({"role": "user", "content": "intervening conversation"})
                    raise ApiError("store_unavailable", 503, reason="response_timeout")
                return super().call(op, **data)

        self.rebuild(LosingPurge())
        self.sign_in()
        self.store.messages = [{"role": "user", "content": "Private owner data"}]
        response = self.client.delete("/api/account", json={},
                                      headers={"Origin": ORIGIN, "X-CSRF-Token": "synthetic-csrf"},
                                      **self.base)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "erasure_uncertain")
        self.assertEqual([op for op, _ in self.store.calls].count("purge"), 1)
        self.assertEqual(self.store.messages, [{"role": "user", "content": "intervening conversation"}])
        reopened = self.client.get("/api/conversations/" + CONVERSATION_ID, **self.base)
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(reopened.json["messages"], self.store.messages)
        self.assertEqual(len(self.store.other_messages), 1)

    def test_boolean_schema_version_stays_unready(self):
        class BoolSchema(FakeStore):
            def call(self, op, **data):
                if op == "check":
                    self.calls.append((op, copy.deepcopy(data)))
                    return {"status": "available", "schema_version": True, "receipt_minted": False}
                return super().call(op, **data)

        self.rebuild(BoolSchema())
        self.sign_in()
        response = self.client.get("/readyz", **self.base)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ready"]["store"])
        self.assertEqual(response.json["store_failure"], "schema_mismatch")

    def test_submitted_purge_error_stays_uncertain_without_retry(self):
        class SubmittedPurge(FakeStore):
            def call(self, op, **data):
                if op == "purge":
                    self.calls.append((op, copy.deepcopy(data)))
                    self.messages.clear()
                    self.messages.append({"role": "user", "content": "intervening conversation"})
                    raise ApiError("store_unavailable", 503, reason="invalid_response", submitted=True)
                return super().call(op, **data)

        self.rebuild(SubmittedPurge())
        self.sign_in()
        self.store.messages = [{"role": "user", "content": "Private owner data"}]
        response = self.client.delete("/api/account", json={},
                                      headers={"Origin": ORIGIN, "X-CSRF-Token": "synthetic-csrf"},
                                      **self.base)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "erasure_uncertain")
        self.assertEqual([op for op, _ in self.store.calls].count("purge"), 1)
        self.assertEqual(self.store.messages, [{"role": "user", "content": "intervening conversation"}])
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


class ScriptedSocket:
    def __init__(self, harness, mode):
        self.harness = harness
        self.mode = mode
        self.timeout = None
        self.sent = b""
        self.options = {}
        self.session_reused = False

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, level, option, value):
        kind = "segment" if option == socket.TCP_MAXSEG else "window"
        self.harness.events.append((kind, level, option, value))
        if getattr(self.harness, "reject_segment", False):
            raise OSError("unsupported")
        self.options[(level, option)] = value

    def getsockopt(self, level, option):
        if option == socket.TCP_MAXSEG:
            reported = getattr(self.harness, "reported_segment", None)
            if reported is not None:
                return reported
        return self.options[(level, option)]

    def connect(self, address):
        self.harness.events.append(("connect", address[0]))
        if self.mode == "connect-timeout":
            self.harness.advance(self.timeout)
            raise TimeoutError("synthetic-connect")
        if self.mode == "refuse":
            raise ConnectionRefusedError("synthetic-refuse")

    def sendall(self, data):
        if self.mode == "reset-on-send":
            raise ConnectionResetError("synthetic-reset-after-send")
        self.sent += data
        self.harness.sent.append(data)
        self.harness.events.append(("send",))
        if self.mode in {"lose-response", "close-early"}:
            if self.harness.events.count(("send",)) == 1:
                self.harness.conversations.clear()
                self.harness.conversations.add("new-conversation")
            else:
                self.harness.conversations.clear()

    def recv(self, _size):
        if self.mode == "close-early":
            return b""
        if self.mode == "ok-hold" and not self.harness.payload:
            raise TimeoutError("synthetic-held-open")
        if self.mode == "short":
            if not self.harness.payload:
                return b""
            chunk = self.harness.payload
            self.harness.payload = b""
            return chunk
        if self.mode != "ok" and self.mode != "ok-hold":
            raise TimeoutError("synthetic-read")
        if not self.harness.payload:
            return b""
        chunk = self.harness.payload[:16384]
        self.harness.payload = self.harness.payload[len(chunk):]
        return chunk

    def close(self):
        self.harness.events.append(("close",))


class ScriptedContext:
    def __init__(self, harness):
        self.harness = harness
        self.verify_mode = ssl.CERT_REQUIRED
        self.check_hostname = True

    def wrap_socket(self, sock, server_hostname):
        self.harness.events.append(("tls", server_hostname))
        if sock.mode == "tls-timeout":
            self.harness.advance(sock.timeout)
            raise TimeoutError("synthetic-tls")
        if sock.mode == "tls-cert":
            raise ssl.SSLCertVerificationError("certificate verify failed")
        if sock.mode == "tls-reset":
            raise ConnectionResetError("synthetic-reset")
        if sock.mode == "tls-secret":
            raise ssl.SSLError("failed " + ("b" * 32))
        return ScriptedSocket(self.harness, sock.mode)


class TransportHarness:
    def __init__(self, modes, addresses=2, ipv6_modes=None):
        self.modes = list(modes)
        self.ipv6_modes = list(ipv6_modes or [])
        self.addresses = addresses
        self.events = []
        self.sent = []
        self.now = 0
        self.conversations = {"existing-conversation"}
        self.payload = b""

    def advance(self, seconds):
        self.now += seconds

    def getaddrinfo(self, host, port, family, socktype):
        self.events.append(("dns", family, host, port, socktype))
        if family == socket.AF_INET6:
            if not self.ipv6_modes:
                raise socket.gaierror(socket.EAI_NONAME, "synthetic-ipv6")
            return [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("2001:db8::1", 443, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (f"203.0.113.{index}", 443))
                for index in range(1, self.addresses + 1)]

    def socket(self, af, socktype, proto):
        if af == socket.AF_INET6:
            if not self.ipv6_modes:
                raise AssertionError("unexpected ipv6 connection")
            return ScriptedSocket(self, self.ipv6_modes.pop(0))
        if not self.modes:
            raise AssertionError("unexpected extra connection")
        return ScriptedSocket(self, self.modes.pop(0))

    def context(self):
        return ScriptedContext(self)

    def patch(self):
        return mock.patch.multiple(
            "app.socket", getaddrinfo=self.getaddrinfo, socket=self.socket,
        ), mock.patch("app.ssl.create_default_context", self.context)

    def response(self, body):
        encoded = body.encode()
        self.payload = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(encoded)).encode() + b"\r\nConnection: close\r\n\r\n" + encoded
        )


class StoreTransportTests(unittest.TestCase):
    def test_store_url_allows_dedicated_custom_domain_and_workers_dev(self):
        for url in ("https://agents-store.a-11-oy.com",
                    "https://example.account.workers.dev"):
            with self.subTest(url=url):
                self.assertTrue(valid_store_url(url))
                self.assertTrue(Store(url, "b" * 32).configured)

    def test_store_url_rejects_other_domains_and_url_decorations(self):
        for url in (
            "http://agents-store.a-11-oy.com",
            "https://agents-store.a-11-oy.com/",
            "https://agents-store.a-11-oy.com/v1/store",
            "https://agents-store.a-11-oy.com?op=check",
            "https://agents-store.a-11-oy.com#fragment",
            "https://agents-store.a-11-oy.com:443",
            "https://user:pass@agents-store.a-11-oy.com",
            "https://agents-store.a-11-oy.com.evil.example",
            "https://evil-agents-store.a-11-oy.com",
            "https://agents-store.a-11-oy.net",
            "https://agents-store.a-11-oy.com.",
            "https://AGENTS-STORE.a-11-oy.com",
            "https://agents-store.a-11-oy.com\n",
            "https://agents-store.a-11-oy.com\t",
            "https://example.account.workers.dev/path",
        ):
            with self.subTest(url=url):
                self.assertFalse(valid_store_url(url))
                self.assertFalse(Store(url, "b" * 32).configured)

    def _store(self, harness, **budgets):
        store = Store("https://example.account.workers.dev", "b" * 32, **budgets)
        store._clock = lambda: harness.now
        return store

    def _run(self, harness, store, op="check", **data):
        stderr = io.StringIO()
        patches = harness.patch()
        with patches[0], patches[1], mock.patch("sys.stderr", stderr):
            try:
                body = store.call(op, **data)
            except ApiError as exc:
                body = exc
        return body, stderr.getvalue()

    def test_missing_configuration_does_not_connect(self):
        with self.assertRaises(ApiError) as caught:
            Store("http://example.account.workers.dev", "b" * 32).call("check")
        self.assertEqual(caught.exception.reason, "missing_configuration")

    def test_invalid_response_is_distinct_from_transport_failure(self):
        with self.assertRaises(ApiError) as caught:
            decode_store_response(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nnope")
        self.assertEqual(caught.exception.reason, "invalid_response")

    def test_success_logs_split_phases_without_secrets_or_resolver_mutation(self):
        original = urllib3_connection.allowed_gai_family
        harness = TransportHarness(["ok"])
        harness.addresses = 1
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://user:proxy-secret-value@203.0.113.9:8080"}):
            body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        self.assertIn("phase=dns", logs)
        self.assertIn("phase=tcp", logs)
        self.assertIn("phase=tls", logs)
        self.assertIn("phase=response", logs)
        self.assertIn("proxy_env=HTTPS_PROXY", logs)
        self.assertIn("trust_env=false", logs)
        self.assertIn("direct=true", logs)
        self.assertNotIn("proxy-secret-value", logs)
        self.assertNotIn("b" * 32, logs)
        self.assertRegex(logs, r"correlation=[0-9a-f]{8}")
        self.assertIs(urllib3_connection.allowed_gai_family, original)
        self.assertEqual(harness.events.count(("send",)), 1)

    def test_chunked_response_is_decoded_without_waiting_for_close(self):
        harness = TransportHarness(["ok-hold"])
        harness.addresses = 1
        body = b'{"status":"available","schema_version":1,"receipt_minted":false}'
        chunk = format(len(body), "x").encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
        harness.payload = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
            + chunk
        )
        decoded, _logs = self._run(harness, self._store(harness))
        self.assertEqual(decoded["receipt_minted"], False)
        self.assertEqual(harness.events.count(("send",)), 1)

    def test_completed_response_does_not_wait_for_the_server_to_close(self):
        harness = TransportHarness(["ok-hold"])
        harness.addresses = 1
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, _logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(harness.events.count(("send",)), 1)

    def test_ipv6_stall_stays_inside_one_second_and_ipv4_still_runs(self):
        harness = TransportHarness(["ok"], addresses=1, ipv6_modes=["connect-timeout"])
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, logs = self._run(harness, self._store(harness, connect_budget=8, address_budget=3))
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(harness.now, 1)
        self.assertEqual([event for event in harness.events if event[0] == "connect"],
                         [("connect", "2001:db8::1"), ("connect", "203.0.113.1")])
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertIn("result=connect_failure", logs)
        self.assertNotIn("b" * 32, logs)

    def test_transport_fallback_uses_the_next_address_before_submission(self):
        harness = TransportHarness(["tls-timeout", "ok"])
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual([event for event in harness.events if event[0] == "connect"],
                         [("connect", "203.0.113.1"), ("connect", "203.0.113.2")])
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertIn("result=tls_failure", logs)
        self.assertIn("tls_class=handshake_timeout", logs)
        self.assertIn("detail=synthetic-tls", logs)
        self.assertIn("sni=example.account.workers.dev", logs)
        self.assertRegex(logs, r"clock=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        self.assertNotIn("b" * 32, logs)

    def test_tls_classes_stay_distinct_and_verification_stays_enabled(self):
        store = Store("https://example.account.workers.dev", "b" * 32)
        self.assertEqual(store._classify_tls(TimeoutError("synthetic-tls")), "handshake_timeout")
        self.assertEqual(
            store._classify_tls(ssl.SSLCertVerificationError("certificate verify failed")),
            "certificate_validation",
        )
        self.assertEqual(
            store._classify_tls(ssl.SSLError("CERTIFICATE_VERIFY_FAILED")),
            "certificate_validation",
        )
        self.assertEqual(store._classify_tls(ConnectionResetError(104, "synthetic-reset")), "connection_reset")
        self.assertEqual(store._classify_tls(OSError(10054, "forcibly closed")), "connection_reset")
        self.assertEqual(store._classify_tls(ssl.SSLError("unexpected eof")), "tls_other")
        self.assertEqual(store._sanitize_tls_detail(ssl.SSLError("failed " + ("b" * 32))), "failed_redacted")
        context = ssl.create_default_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_certificate_validation_retries_the_next_address_before_send(self):
        harness = TransportHarness(["tls-cert", "ok"])
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual([event for event in harness.events if event[0] == "connect"],
                         [("connect", "203.0.113.1"), ("connect", "203.0.113.2")])
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertIn("tls_class=certificate_validation", logs)
        self.assertIn("detail=certificate_verify_failed", logs)
        self.assertIn("error=SSLCertVerificationError", logs)
        self.assertIn("sni=example.account.workers.dev", logs)
        self.assertRegex(logs, r"ca_sha256=(unavailable|unreadable|[0-9a-f]{64})")

    def test_pre_send_connection_reset_retries_the_next_address(self):
        harness = TransportHarness(["tls-reset", "ok"])
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertEqual([event[1] for event in harness.events if event[0] == "connect"],
                         ["203.0.113.1", "203.0.113.2"])
        self.assertIn("tls_class=connection_reset", logs)
        self.assertIn("detail=synthetic-reset", logs)
        self.assertIn("phase=tls", logs)

    def test_tls_detail_redacts_the_store_secret(self):
        harness = TransportHarness(["tls-secret"], addresses=1)
        body, logs = self._run(harness, self._store(harness))
        self.assertIsInstance(body, ApiError)
        self.assertEqual(body.reason, "tls_failure")
        self.assertFalse(body.submitted)
        self.assertEqual(harness.events.count(("send",)), 0)
        self.assertIn("tls_class=tls_other", logs)
        self.assertIn("detail=failed_redacted", logs)
        self.assertNotIn("b" * 32, logs)

    def test_connection_reset_after_send_does_not_try_another_address(self):
        harness = TransportHarness(["reset-on-send", "ok"], addresses=2)
        body, logs = self._run(harness, self._store(harness), "purge", owner="ab" * 32)
        self.assertIsInstance(body, ApiError)
        self.assertEqual(body.reason, "response_timeout")
        self.assertTrue(body.submitted)
        self.assertEqual([event for event in harness.events if event[0] == "connect"],
                         [("connect", "203.0.113.1")])
        self.assertNotIn(("send",), harness.events)
        self.assertIn("phase=response", logs)
        self.assertIn("result=response_timeout", logs)
        self.assertIn("error=ConnectionResetError", logs)
        self.assertNotIn("tls_class=connection_reset", logs)
        self.assertNotIn("b" * 32, logs)

    def test_segment_ceiling_is_applied_before_connect_and_verification_stays(self):
        harness = TransportHarness(["ok"], addresses=1)
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        segment = ("segment", socket.IPPROTO_TCP, socket.TCP_MAXSEG, TCP_SEGMENT_CEILING)
        self.assertIn(segment, harness.events)
        self.assertLess(harness.events.index(segment),
                        harness.events.index(("connect", "203.0.113.1")))
        self.assertEqual([event for event in harness.events if event[0] == "tls"],
                         [("tls", "example.account.workers.dev")])
        self.assertIn(f"segment={TCP_SEGMENT_CEILING}", logs)
        self.assertEqual(harness.events.count(("send",)), 1)
        context = ssl.create_default_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_handshake_window_is_small_until_the_certificate_verifies(self):
        option = 10
        with mock.patch.object(socket, "TCP_WINDOW_CLAMP", option, create=True):
            harness = TransportHarness(["ok"], addresses=1)
            harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
            body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        narrowed = ("window", socket.IPPROTO_TCP, option, HANDSHAKE_WINDOW_BYTES)
        widened = ("window", socket.IPPROTO_TCP, option, RESPONSE_WINDOW_BYTES)
        self.assertLess(harness.events.index(narrowed),
                        harness.events.index(("connect", "203.0.113.1")))
        self.assertLess(harness.events.index(("tls", "example.account.workers.dev")),
                        harness.events.index(widened))
        self.assertLess(harness.events.index(widened), harness.events.index(("send",)))
        self.assertIn("window=536", logs)
        self.assertEqual(harness.events.count(("send",)), 1)
        context = ssl.create_default_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_tls_timeout_does_not_widen_the_window_or_send(self):
        option = 10
        with mock.patch.object(socket, "TCP_WINDOW_CLAMP", option, create=True):
            harness = TransportHarness(["tls-timeout"], addresses=1)
            body, logs = self._run(harness, self._store(harness))
        self.assertIsInstance(body, ApiError)
        self.assertEqual(body.reason, "tls_failure")
        self.assertFalse(body.submitted)
        self.assertNotIn(("send",), harness.events)
        self.assertNotIn(("window", socket.IPPROTO_TCP, option, RESPONSE_WINDOW_BYTES),
                         harness.events)
        self.assertIn(("window", socket.IPPROTO_TCP, option, HANDSHAKE_WINDOW_BYTES),
                      harness.events)
        self.assertIn("window=536", logs)
        self.assertIn("result=tls_failure", logs)

    def test_rejected_segment_option_still_uses_verified_tls_once(self):
        harness = TransportHarness(["ok"], addresses=1)
        harness.reject_segment = True
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        self.assertIn("segment=unsupported", logs)
        self.assertEqual([event for event in harness.events if event[0] == "tls"],
                         [("tls", "example.account.workers.dev")])
        self.assertEqual(harness.events.count(("send",)), 1)

    def test_ignored_segment_ceiling_does_not_add_a_send(self):
        harness = TransportHarness(["ok"], addresses=1)
        harness.reported_segment = TCP_SEGMENT_CEILING + 260
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        body, logs = self._run(harness, self._store(harness))
        self.assertEqual(body["schema_version"], 1)
        self.assertIn(f"segment=rejected:{TCP_SEGMENT_CEILING + 260}", logs)
        self.assertEqual(harness.events.count(("send",)), 1)

    def test_redirect_is_not_followed(self):
        harness = TransportHarness(["ok"])
        harness.addresses = 1
        harness.payload = (
            b"HTTP/1.1 302 Found\r\nLocation: https://evil.example/v1/store\r\n"
            b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        )
        body, _logs = self._run(harness, self._store(harness), "purge", owner="ab" * 32)
        self.assertIsInstance(body, ApiError)
        self.assertEqual(body.reason, "invalid_response")
        self.assertTrue(body.submitted)
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertEqual([event for event in harness.events if event[0] == "connect"],
                         [("connect", "203.0.113.1")])

    def test_overall_connect_budget_stops_later_addresses(self):
        harness = TransportHarness(["connect-timeout", "connect-timeout", "connect-timeout"], addresses=3)
        store = self._store(harness, connect_budget=5, address_budget=3)
        body, logs = self._run(harness, store)
        self.assertIsInstance(body, ApiError)
        self.assertEqual(body.reason, "connect_failure")
        self.assertEqual([event[1] for event in harness.events if event[0] == "connect"],
                         ["203.0.113.1", "203.0.113.2"])
        self.assertNotIn(("send",), harness.events)
        self.assertIn("phase=tcp", logs)

    def test_purge_lost_response_does_not_retry_or_delete_new_data(self):
        harness = TransportHarness(["lose-response"], addresses=2)
        store = self._store(harness)
        body, logs = self._run(harness, store, "purge", owner="ab" * 32)
        self.assertIsInstance(body, ApiError)
        self.assertEqual(body.reason, "response_timeout")
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertEqual([event for event in harness.events if event[0] == "connect"],
                         [("connect", "203.0.113.1")])
        segment = ("segment", socket.IPPROTO_TCP, socket.TCP_MAXSEG, TCP_SEGMENT_CEILING)
        self.assertLess(harness.events.index(segment),
                        harness.events.index(("connect", "203.0.113.1")))
        self.assertEqual(harness.conversations, {"new-conversation"})
        self.assertIn("phase=response", logs)
        self.assertIn("result=response_timeout", logs)
        self.assertNotIn("b" * 32, logs)

    def test_purge_closed_before_headers_is_uncertain_and_not_replayed(self):
        harness = TransportHarness(["close-early"], addresses=2)
        body, _logs = self._run(harness, self._store(harness), "purge", owner="ab" * 32)
        self.assertIsInstance(body, ApiError)
        self.assertEqual(body.reason, "response_timeout")
        self.assertTrue(body.submitted)
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertEqual(harness.conversations, {"new-conversation"})

    def test_duplicate_content_length_is_rejected(self):
        harness = TransportHarness(["ok"], addresses=2)
        harness.payload = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 1\r\nContent-Length: 1\r\n\r\n{}"
        )
        result, _logs = self._run(harness, self._store(harness), "purge", owner="ab" * 32)
        self.assertIsInstance(result, ApiError)
        self.assertEqual(result.reason, "invalid_response")
        self.assertTrue(result.submitted)
        self.assertEqual(harness.events.count(("send",)), 1)

    def test_short_content_length_is_not_accepted(self):
        harness = TransportHarness(["short"], addresses=2)
        body = b'{"status":"available","schema_version":1,"receipt_minted":false}'
        harness.payload = (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 200\r\n\r\n"
            + body
        )
        result, _logs = self._run(harness, self._store(harness))
        self.assertIsInstance(result, ApiError)
        self.assertEqual(result.reason, "invalid_response")
        self.assertTrue(result.submitted)
        self.assertEqual(harness.events.count(("send",)), 1)

    def test_handshake_comparison_records_split_times_without_a_payload(self):
        option = 10
        with mock.patch.object(socket, "TCP_WINDOW_CLAMP", option, create=True):
            harness = TransportHarness(["ok", "ok"], addresses=1)
            store = self._store(harness)
            stderr = io.StringIO()

            def context():
                harness.advance(0.012)
                base = ScriptedContext(harness)
                original = base.wrap_socket

                def wrap_socket(sock, server_hostname):
                    harness.advance(0.034)
                    peer = original(sock, server_hostname)
                    peer.session_reused = harness.events.count(("tls", server_hostname)) > 1
                    return peer

                base.wrap_socket = wrap_socket
                return base

            with (harness.patch()[0], mock.patch("app.ssl.create_default_context", context),
                  mock.patch("sys.stderr", stderr)):
                store.compare_once()
                store.compare_once()
        logs = stderr.getvalue()
        connects = [event for event in harness.events if event[0] == "connect"]
        self.assertEqual(connects, [("connect", "203.0.113.1"), ("connect", "203.0.113.1")])
        self.assertEqual(harness.events.count(("send",)), 0)
        self.assertEqual(harness.sent, [])
        first_connect = harness.events.index(("connect", "203.0.113.1"))
        second_connect = harness.events.index(("connect", "203.0.113.1"), first_connect + 1)
        segment = ("segment", socket.IPPROTO_TCP, socket.TCP_MAXSEG, TCP_SEGMENT_CEILING)
        window = ("window", socket.IPPROTO_TCP, option, HANDSHAKE_WINDOW_BYTES)
        self.assertNotIn(segment, harness.events[:first_connect])
        self.assertNotIn(window, harness.events[:first_connect])
        self.assertLess(first_connect, harness.events.index(segment))
        self.assertLess(harness.events.index(segment), second_connect)
        self.assertLess(harness.events.index(window), second_connect)
        self.assertEqual(harness.events.count(segment), 1)
        self.assertEqual([event for event in harness.events if event[0] == "tls"], [
            ("tls", "example.account.workers.dev"),
            ("tls", "example.account.workers.dev"),
        ])
        self.assertEqual(logs.count("arm=standard"), 1)
        self.assertEqual(logs.count("arm=custom"), 1)
        self.assertIn("family=ipv4", logs)
        self.assertIn("context_ms=12", logs)
        self.assertIn("handshake_ms=34", logs)
        self.assertIn("session_reused=false", logs)
        self.assertIn("session_reused=true", logs)
        self.assertIn("connection=new", logs)
        self.assertIn("segment=default", logs)
        self.assertIn(f"segment={TCP_SEGMENT_CEILING}", logs)
        self.assertIn("window=default", logs)
        self.assertIn(f"window={HANDSHAKE_WINDOW_BYTES}", logs)
        self.assertIn("sni=example.account.workers.dev", logs)
        self.assertNotIn("203.0.113.1", logs)
        self.assertNotIn("Authorization", logs)
        self.assertNotIn("b" * 32, logs)
        self.assertNotIn("phase=response", logs)
        self.assertEqual(store.connect_budget, 8)
        self.assertEqual(store.address_budget, 3)
        self.assertEqual(store.read_budget, 15)
        verified = ssl.create_default_context()
        self.assertTrue(store._require_verification(verified))

    def test_comparison_refuses_a_context_that_does_not_verify(self):
        harness = TransportHarness(["ok", "ok"], addresses=1)
        store = self._store(harness)

        def context():
            base = ScriptedContext(harness)

            def wrap_socket(_sock, _server_hostname):
                raise AssertionError("disabled verification was used")

            base.verify_mode = ssl.CERT_NONE
            base.wrap_socket = wrap_socket
            return base

        stderr = io.StringIO()
        with (harness.patch()[0], mock.patch("app.ssl.create_default_context", context),
              mock.patch("sys.stderr", stderr)):
            store.compare_once()
        logs = stderr.getvalue()
        self.assertEqual(logs.count("result=refused"), 2)
        self.assertIn("error=VerificationDisabled", logs)
        self.assertNotIn(("tls", "example.account.workers.dev"), harness.events)
        self.assertEqual(harness.events.count(("send",)), 0)
        self.assertEqual([event for event in harness.events if event[0] == "connect"],
                         [("connect", "203.0.113.1"), ("connect", "203.0.113.1")])

    def test_comparison_timeout_does_not_submit_or_replace_the_real_write(self):
        harness = TransportHarness(["tls-timeout", "tls-timeout", "ok"], addresses=1)
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        store = self._store(harness)
        stderr = io.StringIO()
        patches = harness.patch()
        with patches[0], patches[1], mock.patch("sys.stderr", stderr):
            store.compare_once()
            body = store.call("check")
        logs = stderr.getvalue()
        self.assertEqual(body["schema_version"], 1)
        self.assertFalse(getattr(body, "submitted", False))
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertEqual(logs.count("arm=standard"), 1)
        self.assertEqual(logs.count("arm=custom"), 1)
        self.assertIn("context_ms=0", logs)
        self.assertIn("handshake_ms=3000", logs)
        self.assertIn("tls_class=handshake_timeout", logs)
        self.assertNotIn("Authorization", logs)
        self.assertNotIn("b" * 32, logs)
        self.assertNotIn("receipt_minted", logs)
        segment = ("segment", socket.IPPROTO_TCP, socket.TCP_MAXSEG, TCP_SEGMENT_CEILING)
        self.assertEqual(harness.events.count(segment), 2)

    def test_comparison_redacts_a_secret_and_still_sends_the_real_write_once(self):
        harness = TransportHarness(["tls-secret", "ok", "ok"], addresses=1)
        harness.response('{"status":"available","schema_version":1,"receipt_minted":false}')
        store = self._store(harness)
        stderr = io.StringIO()
        patches = harness.patch()
        with patches[0], patches[1], mock.patch("sys.stderr", stderr):
            store.compare_once()
            body = store.call("check")
        logs = stderr.getvalue()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(harness.events.count(("send",)), 1)
        self.assertIn("detail=failed_redacted", logs)
        self.assertIn("arm=custom", logs)
        self.assertIn("result=verified", logs)
        self.assertNotIn("b" * 32, logs)
        self.assertNotIn("Authorization", logs)

    def test_concurrent_readiness_checks_are_not_serialized(self):
        store = Store("https://example.account.workers.dev", "b" * 32)
        state = {"current": 0, "peak": 0}
        lock = threading.Lock()

        def exchange(_op, _data, family, _deadline, _correlation):
            with lock:
                state["current"] += 1
                state["peak"] = max(state["peak"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            if family == socket.AF_INET6:
                raise StoreFailure("connect_failure", False, "tcp", 1, "ConnectionRefusedError")
            return {"status": "available", "schema_version": 1, "receipt_minted": False}

        store._exchange = exchange
        errors = []

        def run():
            try:
                body = store.call("check")
                if body["schema_version"] != 1:
                    errors.append(body)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(state["peak"], 2)


if __name__ == "__main__":
    unittest.main()
