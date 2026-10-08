"""Durable browser acceptance and exact-turn cancellation regressions, all offline."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import uuid

import app
from session_control import SessionControl
import studio
import test_studio


class SubmissionTests(unittest.TestCase):
    setUp = test_studio.StudioTests.setUp
    finish = test_studio.StudioTests.finish
    request = test_studio.StudioTests.request

    def body(self, action="send", message="A question"):
        return {"action": action, "message": message, "client_submission_id": str(uuid.uuid4())}

    def test_writes_require_submission_identity(self):
        for action in ("start", "send"):
            for value in (None, "", "not-a-uuid", 1):
                with self.subTest(action=action, value=value), self.assertRaises(studio.StudioError):
                    self.instance.submit({"action": action, "message": "No write", "client_submission_id": value})
        self.assertEqual(self.calls, [])

    def test_completed_duplicate_replays_original_and_conflict_is_denied(self):
        body = self.body()
        first = self.instance.submit(body)
        self.finish(first)
        self.assertEqual(self.instance.submit(body), first)
        self.assertEqual(len(self.calls), 1)
        for changed in (dict(body, message="Different"), dict(body, action="start"), dict(body, session_id="sess_other")):
            with self.assertRaises(studio.StudioError) as error:
                self.instance.submit(changed)
            self.assertEqual(error.exception.status, 409)

    def test_running_duplicate_replays_original(self):
        self.gate = threading.Event()
        body = self.body()
        first = self.instance.submit(body)
        try:
            self.assertEqual(self.instance.submit(body), first)
            self.assertEqual(self.instance.state()["busy_job_id"], first)
        finally:
            self.gate.set()
            self.finish(first)
        self.assertEqual(len(self.calls), 1)

    def test_get_recovers_lost_acceptance_without_receipt_write(self):
        body = self.body()
        job_id = self.instance.submit(body)
        self.finish(job_id)
        files = list((self.root / "data").glob("*.json"))
        before = {path.name: path.read_bytes() for path in files}
        status, _, raw = self.request("GET", "/api/submissions/" + body["client_submission_id"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["job_id"], job_id)
        self.assertEqual(before, {path.name: path.read_bytes() for path in files})
        self.assertEqual(len(self.calls), 1)

    def test_restart_and_pruning_keep_submission_tombstone(self):
        body = self.body()
        job_id = self.instance.submit(body)
        self.finish(job_id)
        with mock.patch.object(studio, "MAX_JOBS", 0):
            self.instance._prune()
        self.assertFalse(self.instance.submission(body["client_submission_id"])["retained"])
        runner = mock.Mock(side_effect=AssertionError("Must not dispatch again"))
        recovered = studio.Studio(self.root / "data", self.state_path, runner)
        self.assertEqual(recovered.submit(body), job_id)
        self.assertFalse(recovered.submission(body["client_submission_id"])["retained"])
        runner.assert_not_called()

    def test_mapping_persistence_failure_precedes_dispatch(self):
        body = self.body()
        real = studio.atomic_json
        def write(path, value):
            if path.name == "submissions.json":
                raise OSError("Synthetic disk failure")
            return real(path, value)
        with mock.patch.object(studio, "atomic_json", side_effect=write), self.assertRaises(OSError):
            self.instance.submit(body)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.instance.busy_job_id)
        self.assertNotIn(body["client_submission_id"], self.instance.submissions)

    def test_submission_capacity_fails_closed(self):
        with mock.patch.object(studio, "MAX_SUBMISSIONS", 0), self.assertRaises(studio.StudioError) as error:
            self.instance.submit(self.body())
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.calls, [])

    def test_cancel_requires_observed_root_and_only_dispatches_once(self):
        gate, ready = threading.Event(), threading.Event()
        def runner(job, emit):
            emit("event", {"type": "agent.session.turn.started", "turn": {"id": "turn_current"}})
            ready.set()
            gate.wait(3)
            emit("event", {"type": "agent.session.turn.cancelled", "turn": {"id": "turn_current"}})
            return 1, "Partial answer"
        cancel_runner = mock.Mock()
        self.instance.runner = runner
        self.instance.cancel_runner = cancel_runner
        job_id = self.instance.submit(self.body())
        try:
            self.assertTrue(ready.wait(2))
            self.assertTrue(self.instance.job(job_id, 0)["can_cancel"])
            response = self.instance.cancel(job_id, {})
            self.assertEqual(response["cancellation"]["status"], "requested")
            self.instance.cancel(job_id, {})
            cancel_runner.assert_called_once()
            self.assertEqual(cancel_runner.call_args.args[1], {"action": "cancel", "session_id": "sess_saved", "turn_id": "turn_current"})
        finally:
            gate.set()
            job = self.finish(job_id)
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["result"]["partial"])
        self.assertEqual(job["cancellation"]["status"], "confirmed")

    def test_cancel_rejects_unknown_or_terminal_job_and_arbitrary_target(self):
        with self.assertRaises(studio.StudioError) as error:
            self.instance.cancel("0" * 32, {})
        self.assertEqual(error.exception.status, 404)
        job_id = self.instance.submit(self.body())
        self.finish(job_id)
        for body in ({}, {"session_id": "sess_other"}):
            with self.assertRaises(studio.StudioError):
                self.instance.cancel(job_id, body)

    def test_partial_output_uses_authoritative_done_and_retained_deltas(self):
        events = [{"type": "event", "data": data} for data in [
            {"type": "agent.session.turn.output_text.delta", "item_id": "a", "delta": "Draft"},
            {"type": "agent.session.turn.output_text.done", "item_id": "a", "text": "Corrected"},
            {"type": "agent.session.turn.output_text.delta", "item_id": "b", "delta": "Unfinished "},
            {"type": "agent.session.turn.output_text.delta", "item_id": "b", "delta": "part"},
        ]]
        self.assertEqual(studio.partial_text(events), "Corrected\nUnfinished part")

    def test_real_provider_root_events_enable_stop_and_persist_exact_identity(self):
        for event_type in ("agent.session.turn.created", "agent.session.turn.in_progress"):
            with self.subTest(event_type=event_type):
                gate, ready = threading.Event(), threading.Event()
                def runner(job, emit):
                    emit("event", {"type": event_type, "session_id": "sess_saved", "turn": {
                        "id": "turn_real", "session_id": "sess_saved", "subagent_id": None,
                        "status": "queued" if event_type.endswith("created") else "in_progress"}})
                    ready.set()
                    gate.wait(3)
                    return 0, "Completed"
                self.instance.runner = runner
                self.instance.cancel_runner = mock.Mock()
                job_id = self.instance.submit(self.body())
                try:
                    self.assertTrue(ready.wait(2))
                    observed = self.instance.job(job_id, 0)
                    self.assertTrue(observed["can_cancel"])
                    self.assertEqual(observed["root_turn_id"], "turn_real")
                    saved = json.loads((self.root / "data" / ("job-" + job_id + ".json")).read_text())
                    self.assertEqual(saved["root_turn_id"], "turn_real")
                finally:
                    gate.set()
                    self.finish(job_id)


class SessionControlTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.control = SessionControl({"project_id": app.PROJECT_ID}, "Question", self.events.append)
        self.control.observe({"type": "agent.session.created", "session": {"id": "sess_current"}})
        self.control.observe({"type": "agent.session.turn.started", "turn": {"id": "turn_current"}})
        self.request = {"action": "cancel", "session_id": "sess_current", "turn_id": "turn_current"}

    def test_latest_turn_precheck_and_documented_cancel_body(self):
        with mock.patch.object(app, "_latest_turn", side_effect=[{"id": "turn_current", "status": "in_progress"}, {"id": "turn_current", "status": "cancelled"}]), mock.patch.object(app, "_api_json") as api:
            self.control.request_cancel(self.request)
        api.assert_called_once_with("POST", "/agents/sessions/sess_current/events", {"events": [{"type": "agent.session.input.cancel"}]})
        self.assertEqual(self.events[-1]["status"], "confirmed")

    def test_changed_latest_turn_and_completed_race_send_no_cancel(self):
        for latest in ({"id": "turn_other", "status": "in_progress"}, {"id": "turn_current", "status": "completed"}, {"id": "turn_current", "status": "unknown"}):
            with self.subTest(latest=latest), mock.patch.object(app, "_latest_turn", return_value=latest), mock.patch.object(app, "_api_json") as api:
                control = SessionControl({}, "Question", self.events.append, session_id="sess_current")
                control.observe({"type": "agent.session.turn.started", "turn": {"id": "turn_current"}})
                control.request_cancel(self.request)
                api.assert_not_called()
                self.assertEqual(self.events[-1]["status"], "not_active")

    def test_cancel_transport_ambiguity_is_unknown_and_never_retried(self):
        with mock.patch.object(app, "_latest_turn", return_value={"id": "turn_current", "status": "in_progress"}), mock.patch.object(app, "_api_json", side_effect=app.AppError("secret provider error")) as api:
            self.control.request_cancel(self.request)
            self.control.request_cancel(self.request)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(self.events[-1]["status"], "unknown")
        self.assertNotIn("secret provider error", json.dumps(self.events))

    def test_subagent_and_other_session_never_rebind_root(self):
        self.control.observe({"type": "agent.session.turn.started", "session_id": "sess_child", "turn": {"id": "turn_child", "subagent_id": "agent_child"}})
        self.control.observe({"type": "agent.session.turn.cancelled", "session_id": "sess_other", "turn": {"id": "turn_current"}})
        self.assertEqual((self.control.session_id, self.control.turn_id, self.control.terminal), ("sess_current", "turn_current", None))

    def test_idless_terminal_does_not_confirm_an_unobserved_root(self):
        control = SessionControl({}, "Question", self.events.append)
        control.observe({"type": "agent.session.turn.cancelled", "turn": {}})
        self.assertIsNone(control.terminal)
        self.assertEqual(self.events, [])

    def test_real_provider_open_events_require_explicit_root_turn_payload(self):
        for event_type in ("agent.session.turn.created", "agent.session.turn.in_progress"):
            with self.subTest(event_type=event_type):
                control = SessionControl({}, "Question", self.events.append, session_id="sess_current")
                control.observe({"type": event_type, "session_id": "sess_current", "turn_id": "turn_not_enough"})
                self.assertIsNone(control.turn_id)
                control.observe({"type": event_type, "session_id": "sess_current", "turn": {"id": "turn_child", "subagent_id": "child"}})
                self.assertIsNone(control.turn_id)
                control.observe({"type": event_type, "session_id": "sess_current", "turn": {"id": "turn_other", "session_id": "sess_other", "subagent_id": None}})
                self.assertIsNone(control.turn_id)
                control.observe({"type": event_type, "session_id": "sess_current", "turn": {"id": "turn_current", "subagent_id": None, "status": "in_progress"}})
                self.assertEqual(control.turn_id, "turn_current")

    def test_cancelled_followup_reloads_state_and_reconciles_exact_input(self):
        self.control.terminal = "cancelled"
        fresh = {"project_id": app.PROJECT_ID, "pending_submissions": {"sess_current": {"message": "Question", "idempotency_key": "original-key"}}}
        items = [{"type": "message", "role": "user", "turn_id": "turn_current", "content": [{"type": "input_text", "text": "Question"}]}]
        with mock.patch.object(app, "_load_state", return_value=fresh), mock.patch.object(app, "_turn_items", return_value=items), mock.patch.object(app, "_save_state") as save:
            self.control.finish()
        save.assert_called_once()
        self.assertNotIn("sess_current", fresh["pending_submissions"])
        self.assertEqual(fresh["resolved_submissions"]["original-key"]["status"], "cancelled")

    def test_cancelled_turn_with_wrong_saved_input_does_not_clear_pending(self):
        self.control.terminal = "cancelled"
        fresh = {"project_id": app.PROJECT_ID, "pending_submissions": {"sess_current": {"message": "Question", "idempotency_key": "original-key"}}}
        with mock.patch.object(app, "_load_state", return_value=fresh), mock.patch.object(app, "_turn_items", return_value=[]), mock.patch.object(app, "_save_state") as save:
            self.control.finish()
        save.assert_not_called()
        self.assertIn("sess_current", fresh["pending_submissions"])


if __name__ == "__main__":
    unittest.main()
