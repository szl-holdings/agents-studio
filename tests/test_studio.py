"""Exercise local Studio boundaries without contacting a provider."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import studio


class StudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="szl-studio-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "state.json"
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID, "agent_id": "agent_test", "session_id": "sess_saved"}))
        self.calls = []
        self.gate = None
        self.exit_code = 0
        self.new_session = True
        self.output = "Hello from a local test.\n"
        self.env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-studio-credential"})
        self.env.start()
        self.addCleanup(self.env.stop)

        def runner(job, emit):
            self.calls.append(job)
            if self.gate:
                self.gate.wait(3)
            emit("event", {"type": "agent.session.turn.output_text.delta", "delta": "Provisional"})
            emit("log", "Authorization: Bearer sk-fake-studio-credential")
            if job["action"] == "start" and self.new_session:
                self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID, "agent_id": "agent_test", "session_id": "sess_created"}))
                emit("event", {"type": "agent.session.created", "session": {"id": "sess_created"}})
            if job["action"] == "status":
                return self.exit_code, '\n'.join(json.dumps(item) for item in [
                    {"id": "sess_saved", "status": "idle"}, {"latest_turn": {"id": "turn_1", "status": "completed"}},
                    {"data": [{"id": "item_1"}], "has_more": True, "last_id": "item_1"}])
            emit("output", self.output)
            return self.exit_code, self.output

        self.instance = studio.Studio(self.root / "data", self.state_path, runner)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><title>Local test</title>")
        self.server = studio.make_server(self.instance, 0, self.static)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None, token=True, origin=True):
        supplied = {}
        if token:
            supplied["X-Studio-Token"] = self.instance.token
        if origin:
            supplied["Origin"] = self.instance.origin
        if body is not None:
            if isinstance(body, dict) and body.get("action") in ("start", "send"):
                body = dict(body, client_submission_id=body.get("client_submission_id", str(uuid.uuid4())))
            supplied["Content-Type"] = "application/json"
            body = json.dumps(body)
        supplied.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=supplied)
        response = connection.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), raw

    def finish(self, job_id):
        self.instance.worker.join(5)
        self.assertFalse(self.instance.worker.is_alive())
        return self.instance.job(job_id, 0)

    def submit(self, body):
        if body.get("action") in ("start", "send"):
            body = dict(body, client_submission_id=body.get("client_submission_id", str(uuid.uuid4())))
        return self.instance.submit(body)

    def test_static_whitelist_security_headers_and_no_credentials(self):
        status, headers, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertNotIn(self.instance.token.encode(), body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        for path in ("/.new-agent-state.json", "/.studio/server.json", "/../app.py", "/%2e%2e/app.py"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_state_get_requires_token_and_has_no_provider_or_write_effect(self):
        before = {str(path): path.stat().st_mtime_ns for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.request("GET", "/api/state", token=False)[0], 401)
        status, _, body = self.request("GET", "/api/state", origin=False)
        self.assertEqual(status, 200)
        state = json.loads(body)
        self.assertEqual(state["sessions"][0]["id"], "sess_saved")
        self.assertEqual(state["jobs"], [])
        self.assertIsNone(state["busy_job_id"])
        self.assertNotIn(b"sk-fake-studio-credential", body)
        self.assertEqual(self.calls, [])
        after = {str(path): path.stat().st_mtime_ns for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_host_origin_and_missing_token_fail_before_work(self):
        for headers, token, origin, expected in [
            ({"Host": "attacker.invalid"}, True, True, 403),
            ({"Origin": "https://attacker.invalid"}, True, True, 403),
            ({"Origin": "null"}, True, True, 403),
            ({}, False, True, 401), ({}, True, False, 403),
            ({"Sec-Fetch-Site": "cross-site"}, True, True, 403),
        ]:
            with self.subTest(headers=headers, token=token, origin=origin):
                self.assertEqual(self.request("POST", "/api/jobs", {"action": "start", "message": "hello"}, headers, token, origin)[0], expected)
        self.assertEqual(self.calls, [])
        self.assertFalse(list((self.root / "data").glob("job-*.json")))

    def test_request_validation_and_no_external_proxy(self):
        bodies = [{"action": "delete"}, {"action": "start", "message": ""},
                  {"action": "start", "message": "x" * (studio.MAX_MESSAGE + 1)},
                  {"action": "start", "message": "test", "url": "https://example.com"},
                  {"action": "start", "message": "test", "key": "not-accepted"},
                  {"action": "send", "message": "test", "session_id": "../../secret"},
                  {"action": "send", "message": "test", "session_id": "sess_unlisted"}]
        for body in bodies:
            self.assertEqual(self.request("POST", "/api/jobs", body)[0], 400)
        self.assertEqual(self.request("POST", "/api/jobs", {"action": "status"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/jobs", {"action": "status"}, {"Content-Length": str(studio.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.calls, [])

    def test_explicit_start_stream_receipt_and_session(self):
        status, _, body = self.request("POST", "/api/jobs", {"action": "start", "message": "A research question"})
        self.assertEqual(status, 202)
        job = self.finish(json.loads(body)["job_id"])
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["session_id"], "sess_created")
        self.assertEqual(job["result"]["text"], self.output)
        self.assertTrue(any(event["type"] == "event" for event in job["events"]))
        self.assertNotIn("sk-fake-studio-credential", json.dumps(job))
        self.assertFalse(job["receipt"]["signed"])
        saved = json.loads((self.root / "data" / ("job-" + job["id"] + ".json")).read_text())
        payload = {key: value for key, value in saved.items() if key != "receipt"}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(saved["receipt"]["payload_sha256"], digest)

    def test_receipt_hashes_redacted_title_and_message(self):
        job_id = self.submit({"action": "start", "message": "sk-fake-studio-credential secret"})
        job = self.finish(job_id)
        self.assertNotIn("sk-fake-studio-credential", json.dumps(job))
        saved = json.loads((self.root / "data" / ("job-" + job_id + ".json")).read_text())
        payload = {key: value for key, value in saved.items() if key != "receipt"}
        self.assertEqual(saved["receipt"]["payload_sha256"], hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest())

    def test_status_normalizes_documents_and_preserves_pagination(self):
        job = self.finish(self.instance.submit({"action": "status"}))
        self.assertEqual(job["status"], "completed")
        self.assertTrue(job["result"]["items"]["has_more"])
        self.assertEqual(job["result"]["latest_turn"]["id"], "turn_1")

    def test_busy_job_is_recoverable_and_duplicate_write_is_denied(self):
        self.gate = threading.Event()
        try:
            job_id = self.submit({"action": "send", "message": "One message"})
            self.assertEqual(self.instance.state()["busy_job_id"], job_id)
            with self.assertRaises(studio.StudioError) as error:
                self.submit({"action": "send", "message": "Another message"})
            self.assertEqual(error.exception.status, 409)
        finally:
            self.gate.set()
            self.finish(job_id)

    def test_unknown_start_is_not_attributed_to_previous_session(self):
        self.new_session = False
        self.exit_code = 2
        job = self.finish(self.submit({"action": "start", "message": "Unknown"}))
        self.assertEqual(job["status"], "pending")
        self.assertIsNone(job["session_id"])

    def test_pending_start_blocks_another_create(self):
        state = json.loads(self.state_path.read_text())
        state["pending_start"] = {"id": "unknown"}
        self.state_path.write_text(json.dumps(state))
        self.assertEqual(self.request("POST", "/api/jobs", {"action": "start", "message": "Retry"})[0], 409)
        self.assertEqual(self.calls, [])

    def test_pending_followup_requires_identical_message(self):
        state = json.loads(self.state_path.read_text())
        state["pending_submissions"] = {"sess_saved": {"message": "Original"}}
        self.state_path.write_text(json.dumps(state))
        self.assertEqual(self.request("POST", "/api/jobs", {"action": "send", "message": "Different"})[0], 409)
        self.assertEqual(self.calls, [])

    def test_event_cursor_and_output_bounds(self):
        job_id = self.instance.submit({"action": "status"})
        job = self.finish(job_id)
        self.assertEqual(self.instance.job(job_id, job["next_cursor"])["events"], [])
        for _ in range(50):
            self.instance._event(self.instance.jobs[job_id], "log", "x" * 32_000)
        bounded = self.instance.jobs[job_id]
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(bounded["event_characters"], studio.MAX_OUTPUT)

    def test_restart_retains_unobserved_job_as_pending_without_runner(self):
        job_id = "a" * 32
        record = {"id": job_id, "action": "start", "status": "running", "created_at": studio.now(), "events": [], "next_cursor": 0}
        studio.atomic_json(self.root / "data" / ("job-" + job_id + ".json"), record)
        recovered = studio.Studio(self.root / "data", self.state_path, mock.Mock(side_effect=AssertionError("No provider call")))
        self.assertEqual(recovered.job(job_id, 0)["status"], "pending")
        recovered.runner.assert_not_called()

    def test_existing_instance_accepts_only_loopback_verified_identity(self):
        studio.atomic_json(self.root / "data" / "server.json", {"origin": self.instance.origin, "token": self.instance.token, "instance_id": self.instance.instance_id})
        self.assertEqual(studio.existing_instance(self.root / "data"), self.instance.origin + "/#token=" + self.instance.token)
        studio.atomic_json(self.root / "data" / "server.json", {"origin": "http://example.com", "token": self.instance.token, "instance_id": self.instance.instance_id})
        self.assertIsNone(studio.existing_instance(self.root / "data"))


if __name__ == "__main__":
    unittest.main()
