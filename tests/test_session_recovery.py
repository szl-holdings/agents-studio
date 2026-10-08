"""Provider-read-only recovery and bounded history regression fixtures."""

from copy import deepcopy
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import uuid
from contextlib import redirect_stderr, redirect_stdout

import app
import new_agent
import session_recovery as recovery
import studio
import test_studio


def page(data, more=False, cursor=None):
    return {"data": data, "has_more": more, "first_id": data[0]["id"] if data else None,
            "last_id": cursor or (data[-1]["id"] if data else None)}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.message = "Original input"
        self.digest = hashlib.sha256(self.message.encode()).hexdigest()
        self.submission = str(uuid.uuid4())
        self.created = datetime.fromtimestamp(1000, timezone.utc).isoformat()
        self.state = {"project_id": app.PROJECT_ID, "agent_id": "agent_test", "session_id": "sess_saved",
                      "pending_submissions": {"sess_saved": {"message": self.message,
                          "idempotency_key": self.submission, "previous_turn_id": "turn_old"}}}
        self.target = {"id": "a" * 32, "action": "send", "session_id": "sess_saved", "root_turn_id": "turn_target",
                       "message_sha256": self.digest, "created_at": self.created, "client_submission_id": self.submission}
        self.session = {"id": "sess_saved", "agent": {"id": "agent_test"}, "status": "idle", "created_at": 1001}
        self.turn = {"id": "turn_target", "session_id": "sess_saved", "agent_id": "agent_test",
                     "subagent_id": None, "status": "completed"}
        self.items = [{"id": "item_user", "turn_id": "turn_target", "type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": self.message}]},
                      {"id": "item_final", "turn_id": "turn_target", "type": "message", "role": "assistant",
                       "phase": "final_answer", "status": "completed", "content": [{"type": "output_text", "text": "Saved final"}]}]
        self.calls = []
        self.api = mock.patch.object(app, "_api_json", side_effect=self.get)
        self.api.start()
        self.addCleanup(self.api.stop)
        self.save = mock.patch.object(app, "_save_state")
        self.saved = self.save.start()
        self.addCleanup(self.save.stop)

    def get(self, method, path, *args, **kwargs):
        self.assertEqual(method, "GET", "Recovery must never mutate the provider")
        self.calls.append(path)
        parsed = urlsplit(path)
        if parsed.path == "/agents/sessions":
            self.assertEqual(parse_qs(parsed.query)["agent_id"], ["agent_test"])
            return page([self.session])
        if parsed.path == "/agents/sessions/sess_saved":
            return deepcopy(self.session)
        if parsed.path == "/agents/sessions/sess_saved/turns/turn_target":
            return deepcopy(self.turn)
        if parsed.path == "/agents/sessions/sess_saved/turns/turn_target/items":
            return page(deepcopy(self.items))
        if parsed.path == "/agents/sessions/sess_saved/items":
            return page(deepcopy(self.items))
        if parsed.path == "/agents/sessions/sess_saved/turns":
            if parse_qs(parsed.query).get("order") == ["asc"]:
                return page([deepcopy(self.turn)])
            return page([deepcopy(self.turn)])
        raise AssertionError("Unexpected read: " + path)

    def run_recovery(self):
        return recovery.reconcile(self.state, self.target, current_time=1300)

    def unknown_start(self):
        self.target.update(action="start", session_id=None, root_turn_id=None, studio_start_nonce="c" * 64)
        self.session["metadata"] = {"studio_job_id": self.target["id"], "studio_start_nonce": self.target["studio_start_nonce"]}
        self.state["pending_submissions"] = {}
        self.state["pending_start"] = {"id": "marker", "created_at": self.created, "message_sha256": self.digest,
                                       "studio_job_id": self.target["id"], "agent_id": "agent_test", "project_id": app.PROJECT_ID,
                                       "studio_start_nonce": self.target["studio_start_nonce"]}

    def test_exact_completed_turn_reconciles_without_provider_write(self):
        result = self.run_recovery()
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(result["text"], "Saved final")
        self.assertEqual(result["snapshot"]["latest_turn"]["id"], "turn_target")
        saved = self.saved.call_args.args[0]
        self.assertEqual(saved["pending_submissions"], {})
        self.assertEqual(saved["resolved_submissions"][self.submission]["turn_id"], "turn_target")
        self.assertIn("sess_saved", self.state["pending_submissions"], "Do not mutate caller state before persistence")

    def test_new_start_persists_nonce_before_including_it_in_provider_request(self):
        args = argparse.Namespace(message=self.message, raw_events=True, studio_job_id=self.target["id"],
                                  studio_start_nonce="f" * 64)
        def failed_stream(method, path, body):
            self.assertTrue(self.saved.called)
            self.assertEqual(self.state["pending_start"]["studio_start_nonce"], args.studio_start_nonce)
            self.assertEqual(body["metadata"], {"studio_job_id": args.studio_job_id, "studio_start_nonce": args.studio_start_nonce})
            raise app.AppError("Synthetic lost admission response")
        with mock.patch.object(new_agent, "_ensure_agent", return_value="agent_test"), mock.patch.object(app, "_open_stream", side_effect=failed_stream), self.assertRaises(app.AppError):
            new_agent.command_start(args, self.state, {}, "digest")
        self.assertEqual(self.state["pending_start"]["studio_job_id"], self.target["id"])

    def test_failed_and_cancelled_exact_turn_clear_only_matching_pending(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.turn["status"] = status
                result = self.run_recovery()
                self.assertEqual(result["target_status"], status)
                self.assertTrue(result["partial"])
                self.assertTrue(result["pending_resolved"])

    def test_unrelated_completed_latest_does_not_resolve_active_target(self):
        self.turn["status"] = "in_progress"
        result = self.run_recovery()
        self.assertEqual(result["outcome"], "pending")
        self.saved.assert_not_called()

    def test_wrong_input_identity_or_subagent_preserve_markers(self):
        for field, value in (("session_id", "sess_foreign"), ("agent_id", "agent_foreign"), ("subagent_id", "child")):
            with self.subTest(field=field):
                with mock.patch.dict(self.turn, {field: value}):
                    self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.items[0]["content"][0]["text"] = "Another input"
        self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_completed_incomplete_final_retains_pending(self):
        self.items[1]["status"] = "in_progress"
        self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_tool_failure_is_not_promoted_to_success(self):
        self.items.append({"id": "item_tool", "turn_id": "turn_target", "type": "web_search_call", "status": "failed"})
        result = self.run_recovery()
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(result["provider_terminal_status"], "completed")
        self.assertEqual(result["target_status"], "failed")

    def test_different_pending_identity_is_not_cleared(self):
        self.state["pending_submissions"]["sess_saved"]["idempotency_key"] = "another-operation"
        self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_local_persistence_failure_retains_unknown_outcome(self):
        self.saved.side_effect = OSError("Fake disk error containing sk-not-for-output")
        result = self.run_recovery()
        self.assertEqual(result["outcome"], "unavailable")
        self.assertNotIn("sk-not-for-output", result["detail"])
        self.assertIn("sess_saved", self.state["pending_submissions"])

    def test_unique_unknown_start_uses_complete_agent_time_input_match(self):
        self.unknown_start()
        result = self.run_recovery()
        self.assertEqual(result["outcome"], "resolved")
        saved = self.saved.call_args.args[0]
        self.assertNotIn("pending_start", saved)
        self.assertEqual(saved["session_id"], "sess_saved")

    def test_unknown_start_absence_does_not_clear_or_authorize_retry(self):
        self.unknown_start()
        self.session["created_at"] = 900
        self.assertEqual(self.run_recovery()["outcome"], "not_found")
        self.saved.assert_not_called()

    def test_unknown_start_multiple_matches_remains_ambiguous(self):
        self.unknown_start()
        with mock.patch.object(recovery, "start_candidates", return_value=[("sess_saved", "turn_target"), ("sess_other", "turn_other")]):
            self.assertEqual(self.run_recovery()["outcome"], "ambiguous")
        self.saved.assert_not_called()

    def test_future_time_wrong_project_and_wrong_marker_stay_blocked(self):
        self.unknown_start()
        self.assertEqual(recovery.reconcile(self.state, self.target, current_time=900)["outcome"], "unavailable")
        self.state["pending_start"]["project_id"] = "proj_foreign"
        self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.state["pending_start"]["project_id"] = app.PROJECT_ID
        self.state["pending_start"]["studio_job_id"] = "b" * 32
        self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_same_input_time_and_agent_without_matching_metadata_is_not_adopted(self):
        self.unknown_start()
        self.session["metadata"]["studio_start_nonce"] = "d" * 64
        self.assertEqual(self.run_recovery()["outcome"], "not_found")
        self.saved.assert_not_called()

    def test_legacy_start_without_provider_nonce_stays_unavailable(self):
        self.unknown_start()
        self.target.pop("studio_start_nonce")
        self.state["pending_start"].pop("studio_start_nonce")
        self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_start_candidate_incomplete_pages_never_establish_uniqueness(self):
        self.unknown_start()
        get = self.get
        def incomplete(method, path, *args, **kwargs):
            if urlsplit(path).path == "/agents/sessions":
                return page([self.session], True, "repeat_cursor")
            return get(method, path, *args, **kwargs)
        with mock.patch.object(app, "_api_json", side_effect=incomplete):
            self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_unobserved_send_without_original_boundary_stays_blocked(self):
        self.target["root_turn_id"] = None
        self.state["pending_submissions"]["sess_saved"].pop("previous_turn_id")
        self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_unobserved_send_same_input_after_boundary_is_not_adopted(self):
        self.target["root_turn_id"] = None
        old = dict(self.turn, id="turn_old")
        get = self.get
        def with_boundary(method, path, *args, **kwargs):
            if urlsplit(path).path.endswith("/turns") and parse_qs(urlsplit(path).query).get("limit") != ["1"]:
                return page([self.turn, old])
            return get(method, path, *args, **kwargs)
        with mock.patch.object(app, "_api_json", side_effect=with_boundary):
            self.assertEqual(self.run_recovery()["outcome"], "unavailable")
        self.saved.assert_not_called()

    def test_readback_after_profile_save_and_job_crash_can_be_repeated(self):
        self.unknown_start()
        first = self.run_recovery()
        self.assertEqual(first["outcome"], "resolved")
        self.state = self.saved.call_args.args[0]
        result = self.run_recovery()
        self.assertEqual(result["outcome"], "resolved")
        self.assertFalse(any(path.startswith("/agents/sessions?") for path in self.calls[8:]))

    def test_history_validates_cursor_limit_and_exact_agent(self):
        result = recovery.history(self.state, "sess_saved", "opaque:cursor==", 100)
        self.assertEqual(result["after"], "opaque:cursor==")
        self.assertIn("after=opaque%3Acursor%3D%3D", self.calls[-1])
        self.assertIsNone(result["next_cursor"])
        self.saved.assert_not_called()
        for limit in (True, 0, 101, "10"):
            with self.subTest(limit=limit), self.assertRaises(recovery.Unavailable):
                recovery.history(self.state, "sess_saved", limit=limit)
        self.session["agent"]["id"] = "agent_foreign"
        with self.assertRaises(recovery.Unavailable):
            recovery.history(self.state, "sess_saved")

    def test_page_empty_more_repeated_cursor_and_nonboolean_flag_rejected(self):
        for value in (page([], True, "cursor"), {"data": [], "has_more": "false"}, page([{"id": "item_1"}], True, "cursor")):
            with self.subTest(value=value), mock.patch.object(app, "_api_json", return_value=value), self.assertRaises(recovery.Unavailable):
                recovery.Readback().page("/test", after="cursor")


class StudioRecoveryTests(unittest.TestCase):
    setUp = test_studio.StudioTests.setUp
    request = test_studio.StudioTests.request
    finish = test_studio.StudioTests.finish

    def pending(self):
        self.exit_code = 2
        body = {"action": "send", "message": "Original input", "client_submission_id": str(uuid.uuid4())}
        job_id = self.instance.submit(body)
        self.finish(job_id)
        return job_id, body

    def test_history_job_has_bounded_result_and_no_submission_identity(self):
        self.instance.runner = lambda job, emit: (0, json.dumps({"session_id": job["session_id"], "items": page([]),
                                                                "next_cursor": None, "after": job["after"], "limit": job["limit"]}))
        job_id = self.instance.submit({"action": "history", "session_id": "sess_saved", "after": "item_old", "limit": 20})
        job = self.finish(job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["after"], "item_old")
        self.assertEqual(self.instance.submissions, {})
        for extra in ({"message": "Oops"}, {"limit": True}, {"after": "bad?x=2"}, {"client_submission_id": str(uuid.uuid4())}):
            with self.assertRaises(studio.StudioError):
                self.instance.submit({"action": "history", **extra})

    def test_explicit_recovery_creates_separate_job_and_updates_target(self):
        target_id, body = self.pending()
        def runner(job, emit):
            self.assertEqual(job["action"], "reconcile")
            self.assertEqual(job["recovery_request"]["message_sha256"], hashlib.sha256(body["message"].encode()).hexdigest())
            return 0, json.dumps({"target_job_id": target_id, "outcome": "resolved", "target_status": "completed",
                                 "provider_terminal_status": "completed", "pending_resolved": True,
                                 "session_id": "sess_saved", "turn_id": "turn_exact", "text": "Recovered", "partial": False})
        self.instance.runner = runner
        status, _, raw = self.request("POST", f"/api/jobs/{target_id}/reconcile", {})
        self.assertEqual(status, 202)
        recovery_id = json.loads(raw)["job_id"]
        self.assertNotEqual(recovery_id, target_id)
        self.assertEqual(self.finish(recovery_id)["result"]["outcome"], "resolved")
        self.assertEqual(self.instance.job(target_id, 0)["status"], "completed")
        record = self.instance.submission(body["client_submission_id"])
        self.assertEqual(record["provider_terminal_status"], "completed")
        self.assertEqual(record["observed_session_id"], "sess_saved")

    def test_ambiguous_result_retains_target_and_blocks_new_send(self):
        target_id, _ = self.pending()
        self.instance.runner = lambda job, emit: (0, json.dumps({"target_job_id": target_id, "outcome": "ambiguous"}))
        job_id = self.instance.reconcile(target_id, {})
        self.finish(job_id)
        self.assertEqual(self.instance.job(target_id, 0)["status"], "pending")
        with self.assertRaises(studio.StudioError):
            self.instance.submit({"action": "send", "message": "Another", "client_submission_id": str(uuid.uuid4())})

    def test_recovery_validation_and_persistence_failure_dispatch_nothing(self):
        target_id, _ = self.pending()
        self.instance.runner = mock.Mock()
        for target, body in (("0" * 32, {}), (target_id, {"session_id": "sess_foreign"})):
            with self.assertRaises(studio.StudioError):
                self.instance.reconcile(target, body)
        with mock.patch.object(self.instance, "_persist", side_effect=OSError("disk")), self.assertRaises(OSError):
            self.instance.reconcile(target_id, {})
        self.assertIsNone(self.instance.busy_job_id)
        self.instance.runner.assert_not_called()

    def test_tombstone_observed_session_does_not_change_start_fingerprint(self):
        body = {"action": "start", "message": "Hello", "client_submission_id": str(uuid.uuid4())}
        job_id = self.instance.submit(body)
        self.finish(job_id)
        job = self.instance.jobs[job_id]
        job["provider_terminal_status"] = "completed"
        self.instance._submission_status(job)
        with mock.patch.object(studio, "MAX_JOBS", 0):
            self.instance._prune()
        record = self.instance.submission(body["client_submission_id"])
        self.assertFalse(record["retained"])
        self.assertEqual(record["observed_session_id"], "sess_created")
        self.assertIsNone(self.instance.submissions[body["client_submission_id"]]["session_id"])
        self.assertEqual(self.instance.submit(body), job_id)

    def test_recovery_target_summary_is_present_and_get_has_no_writes(self):
        target_id, _ = self.pending()
        self.instance.runner = lambda job, emit: (0, json.dumps({"target_job_id": target_id, "outcome": "pending"}))
        job_id = self.instance.reconcile(target_id, {})
        self.finish(job_id)
        before = {path.name: path.read_bytes() for path in self.instance.data_dir.glob("*.json")}
        summary = next(job for job in self.instance.state()["jobs"] if job["id"] == job_id)
        self.assertEqual(summary["target_job_id"], target_id)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.instance.data_dir.glob("*.json")})


class ReadbackTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-readback-transport-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / "profile.json"
        self.target_path = self.root / "request.json"
        self.requests = []
        self.submission = str(uuid.uuid4())
        self.nonce = "e" * 64
        self.agent, digest = new_agent._definition()
        self.agent["id"] = "agent_test"
        self.state = {"project_id": app.PROJECT_ID, "agent_id": "agent_test", "definition_sha256": digest,
                      "session_id": "sess_saved", "pending_submissions": {"sess_saved": {
                          "message": "Transport input", "idempotency_key": self.submission, "previous_turn_id": "turn_old"}}}
        self.target = {"id": "a" * 32, "action": "send", "session_id": "sess_saved", "root_turn_id": "turn_exact",
                       "message_sha256": hashlib.sha256(b"Transport input").hexdigest(), "client_submission_id": self.submission,
                       "created_at": datetime.fromtimestamp(1000, timezone.utc).isoformat()}
        self.session = {"id": "sess_saved", "agent": {"id": "agent_test"}, "created_at": 1001, "status": "idle",
                        "metadata": {"studio_job_id": self.target["id"], "studio_start_nonce": self.nonce}}
        self.turn = {"id": "turn_exact", "agent_id": "agent_test", "session_id": "sess_saved", "subagent_id": None, "status": "completed"}
        self.items = [{"id": "item_user", "turn_id": "turn_exact", "type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": "Transport input"}]},
                      {"id": "item_answer", "turn_id": "turn_exact", "type": "message", "role": "assistant", "phase": "final_answer",
                       "status": "completed", "content": [{"type": "output_text", "text": "Saved transport answer"}]}]
        scenario = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                scenario.requests.append(("GET", self.path))
                path = urlsplit(self.path).path.removeprefix("/v1")
                routes = {"/agents/agent_test": scenario.agent, "/agents/sessions": page([scenario.session]),
                          "/agents/sessions/sess_saved": scenario.session, "/agents/sessions/sess_saved/turns": page([scenario.turn]),
                          "/agents/sessions/sess_saved/turns/turn_exact": scenario.turn,
                          "/agents/sessions/sess_saved/turns/turn_exact/items": page(scenario.items),
                          "/agents/sessions/sess_saved/items": page(scenario.items)}
                raw = json.dumps(routes.get(path, {"error": "Unexpected fixture route"})).encode()
                self.send_response(200 if path in routes else 404)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                scenario.requests.append(("POST", self.path))
                self.send_error(405)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.shutdown)
        for patcher in (mock.patch.object(new_agent, "STATE_PATH", self.state_path),
                        mock.patch.object(app, "BASE_URL", f"http://127.0.0.1:{self.server.server_port}/v1"),
                        mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-only"})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        self.state_path.write_text(json.dumps(self.state), encoding="utf-8")
        self.target_path.write_text(json.dumps(self.target), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = new_agent.main(list(args))
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertTrue(all(method == "GET" for method, _ in self.requests))
        return json.loads(stdout.getvalue())

    def test_history_cli_real_curl_uses_get_only_and_leaves_profile_unchanged(self):
        result = self.run_cli("history", "--session-id", "sess_saved", "--after", "opaque:cursor==")
        self.assertEqual(result["after"], "opaque:cursor==")
        self.assertEqual(json.loads(self.state_path.read_text()), self.state)
        self.assertIn("after=opaque%3Acursor%3D%3D", self.requests[-1][1])

    def test_reconcile_cli_real_curl_clears_exact_followup_without_submission(self):
        result = self.run_cli("reconcile", "--request-file", str(self.target_path))
        self.assertEqual(result["outcome"], "resolved")
        self.assertEqual(result["text"], "Saved transport answer")
        self.assertEqual(json.loads(self.state_path.read_text())["pending_submissions"], {})

    def test_reconcile_cli_real_curl_adopts_only_nonce_bound_initial_session(self):
        self.target.update(action="start", session_id=None, root_turn_id=None, studio_start_nonce=self.nonce)
        self.state["pending_submissions"] = {}
        self.state["pending_start"] = {"id": "marker", "created_at": self.target["created_at"],
                                       "message_sha256": self.target["message_sha256"], "studio_job_id": self.target["id"],
                                       "studio_start_nonce": self.nonce, "agent_id": "agent_test", "project_id": app.PROJECT_ID}
        result = self.run_cli("reconcile", "--request-file", str(self.target_path))
        self.assertEqual(result["outcome"], "resolved")
        self.assertNotIn("pending_start", json.loads(self.state_path.read_text()))


if __name__ == "__main__":
    unittest.main()
