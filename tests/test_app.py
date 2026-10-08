"""End-to-end CLI tests against a local Agents API stand-in and real curl."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


def sse(*events: dict[str, object]) -> bytes:
    return b"".join(
        b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
        for event in events
    )


class Scenario:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self.followup_posted = threading.Event()
        self.followup_post_failures = 0
        self.agent_error = False
        self.agent_tools: list[dict[str, object]] = [{"type": "web_search", "mode": "live", "context_size": "medium"}]
        self.agent_name = app.AGENT_NAME
        self.session_status = "idle"
        self.latest_turn = {"id": "turn_previous", "status": "completed"}
        self.saved_items: list[dict[str, object]] = [{"id": "latest_item", "status": "failed"}]
        self.start_events = [
            {"type": "agent.session.created", "session": {"id": "sess_test"}},
            {"type": "agent.session.turn.item.done", "item": {
                "id": "tool_test", "type": "web_search_call", "status": "completed"}},
            {"type": "agent.session.turn.output_text.delta", "item_id": "msg_test",
             "output_index": 0, "content_index": 0, "delta": "Hello "},
            {"type": "agent.session.turn.output_text.delta", "item_id": "msg_test",
             "output_index": 0, "content_index": 0, "delta": "world"},
            {"type": "agent.session.turn.output_text.done", "item_id": "msg_test",
             "output_index": 0, "content_index": 0, "text": "Hello world"},
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_test"}},
        ]
        self.followup_events = [
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_previous"}},
            {"type": "agent.session.turn.started", "turn": {"id": "turn_followup"}},
            {"type": "agent.session.turn.output_text.delta", "item_id": "reply_early",
             "delta": "Early draft"},
            {"type": "agent.session.turn.output_text.done", "item_id": "reply_early",
             "text": "Early final"},
            {"type": "agent.session.turn.output_text.done", "turn_id": "turn_followup",
             "item_id": "reply_test",
             "text": "Follow-up reply"},
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_followup"}},
        ]

    def record(self, request: dict[str, object]) -> None:
        with self.lock:
            self.requests.append(request)


class AgentAppIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if not (shutil.which("curl.exe") or shutil.which("curl")):
            self.skipTest("curl executable is unavailable")
        self.scenario = Scenario()
        scenario = self.scenario

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def respond(self, status: int, data: dict[str, object]) -> None:
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def stream(self, events: list[dict[str, object]], *, status: int = 200,
                       wait_for_post: bool = False) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                if wait_for_post and not scenario.followup_posted.wait(timeout=5):
                    return
                try:
                    self.wfile.write(sse(*events))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def dispatch(self, method: str) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(size) if size else b""
                request = {
                    "method": method,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(raw) if raw else None,
                }
                scenario.record(request)
                if (method, self.path) == ("GET", f"/v1/agents/{app.AGENT_ID}"):
                    if scenario.agent_error:
                        self.respond(401, {"error": {"code": "invalid_api_key",
                                                     "message": "Test key rejected"}})
                    else:
                        self.respond(200, {"id": app.AGENT_ID,
                                           "name": scenario.agent_name,
                                           "tools": scenario.agent_tools,
                                           "multi_agent": {"enabled": False}})
                elif (method, self.path) == ("POST", "/v1/agents/sessions"):
                    self.stream(scenario.start_events, status=201)
                elif (method, self.path) == ("GET", "/v1/agents/sessions/sess_test/events?stream=true"):
                    self.stream(scenario.followup_events, wait_for_post=True)
                elif (method, self.path) == ("POST", "/v1/agents/sessions/sess_test/events"):
                    scenario.followup_posted.set()
                    if scenario.followup_post_failures:
                        scenario.followup_post_failures -= 1
                        self.respond(503, {"error": {"code": "temporarily_unavailable",
                                                      "message": "Try again"}})
                    else:
                        self.respond(202, {"data": []})
                elif (method, self.path) == ("GET", "/v1/agents/sessions/sess_test"):
                    self.respond(200, {"id": "sess_test", "status": scenario.session_status,
                                       "required_actions": [
                        {"id": "action_test", "type": "function_call"}]})
                elif (method, self.path) == ("GET", "/v1/agents/sessions/sess_test/turns?order=desc&limit=1"):
                    self.respond(200, {"data": [scenario.latest_turn]})
                elif (method, self.path) == ("GET", "/v1/agents/sessions/sess_test/items?order=desc&limit=100"):
                    self.respond(200, {"data": scenario.saved_items})
                else:
                    self.respond(404, {"error": {"message": "Unexpected test route"}})

            def do_GET(self) -> None:
                self.dispatch("GET")

            def do_POST(self) -> None:
                self.dispatch("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server_thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.tempdir = tempfile.TemporaryDirectory(prefix="agents-api-test-")
        self.addCleanup(self.tempdir.cleanup)
        self.state_path = Path(self.tempdir.name) / "state.json"
        base = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(app, "BASE_URL", base).start()
        mock.patch.object(app, "STATE_PATH", self.state_path).start()
        # Exercise bearer-token characters and harmless whitespace from a paste.
        mock.patch.dict(os.environ, {"OPENAI_API_KEY": "  sk-test+/~==  "}).start()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = app.main(list(args))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_start_rejects_documentation_key_placeholders_before_any_request(self) -> None:
        for placeholder in ("<your-project-API-key>", "<project API key>"):
            with self.subTest(placeholder=placeholder):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": placeholder}):
                    code, stdout, stderr = self.run_cli("start")
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("placeholder", stderr.lower())
                self.assertEqual(self.scenario.requests, [])
                self.assertFalse(self.state_path.exists())

    def test_send_reports_key_placeholder_before_missing_session(self) -> None:
        for placeholder in ("<your-project-API-key>", "<project API key>"):
            with self.subTest(placeholder=placeholder):
                with mock.patch.dict(os.environ, {"OPENAI_API_KEY": placeholder}):
                    code, stdout, stderr = self.run_cli(
                        "send", "--message", "What can you help me research today?"
                    )
                self.assertEqual(code, 1)
                self.assertEqual(stdout, "")
                self.assertIn("placeholder", stderr.lower())
                self.assertNotIn("session ID is required", stderr)
                self.assertEqual(self.scenario.requests, [])
                self.assertFalse(self.state_path.exists())

    def test_start_reuses_saved_agent_with_only_requested_overrides_and_streams(self) -> None:
        code, stdout, stderr = self.run_cli("start", "--message", "Initial question")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Hello world\n")
        self.assertIn(f"Verified saved agent: {app.AGENT_NAME} ({app.AGENT_ID})", stderr)
        self.assertIn("Session ID: sess_test", stderr)
        self.assertIn("[agent.session.turn.item.done]", stderr)
        self.assertIn("tool=web_search_call status=completed id=tool_test", stderr)
        self.assertEqual(json.loads(self.state_path.read_text())["session_id"], "sess_test")
        lookup, start = self.scenario.requests
        self.assertEqual(lookup["method"], "GET")
        self.assertEqual(lookup["path"], f"/v1/agents/{app.AGENT_ID}")
        self.assertEqual(lookup["headers"]["OpenAI-Project"], app.PROJECT_ID)
        self.assertEqual(lookup["headers"]["OpenAI-Beta"], "agents=v1")
        self.assertEqual(lookup["headers"]["Authorization"], "Bearer sk-test+/~==")
        agent = json.loads(app.AGENT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(agent), {"model", "instructions", "reasoning", "text"})
        self.assertEqual(start["body"], {"agent_id": app.AGENT_ID, "agent": agent,
                                         "environment": {"type": "none"},
                                         "input": "Initial question", "stream": True})
        self.assertEqual(start["headers"]["Accept"], "text/event-stream")
        self.assertEqual(json.loads(self.state_path.read_text())["agent_id"], app.AGENT_ID)

        code, _, stderr = self.run_cli("start", "--message", "Second session")
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"Verified saved agent: {app.AGENT_NAME}", stderr)
        self.assertEqual([request["path"] for request in self.scenario.requests].count(
            f"/v1/agents/{app.AGENT_ID}"), 2)
        self.assertFalse(any(request["path"] == "/v1/agents" for request in self.scenario.requests))

    def test_send_subscribes_before_posting_and_uses_idempotency_key(self) -> None:
        state = {"project_id": app.PROJECT_ID, "session_id": "sess_test"}
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        code, stdout, stderr = self.run_cli("send", "--message", "Another question",
                                            "--idempotency-key", "retry-123")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Early final\nFollow-up reply\n")
        self.assertIn("[text delta] Early draft", stderr)
        self.assertTrue(self.scenario.followup_posted.is_set())
        session, turns, subscribe, post = self.scenario.requests
        self.assertEqual(session["path"], "/v1/agents/sessions/sess_test")
        self.assertEqual(turns["path"], "/v1/agents/sessions/sess_test/turns?order=desc&limit=1")
        self.assertEqual(subscribe["method"], "GET")
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["headers"]["Idempotency-Key"], "retry-123")
        self.assertEqual(post["body"], {"events": [{"type": "agent.session.input.message",
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "Another question"}]}]}]})
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["last_turn_id"], "turn_followup")
        self.assertNotIn("sess_test", saved.get("pending_submissions", {}))

    def test_stale_nullable_output_is_discarded_and_new_final_is_read_from_items(self) -> None:
        message = "Question for a new turn"
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test"}), encoding="utf-8")
        self.scenario.followup_events = [
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_previous"}},
            {"type": "agent.session.turn.output_text.done", "turn_id": None,
             "item_id": "old_reply", "text": "Old turn answer"},
            {"type": "agent.session.turn.started", "turn": {"id": "turn_followup"}},
            {"type": "agent.session.turn.output_text.done", "turn_id": "turn_followup",
             "item_id": "new_stream_reply", "text": "Unverified streamed answer"},
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_followup"}},
        ]
        self.scenario.saved_items = [
            {"id": "new_final", "turn_id": "turn_followup", "type": "message",
             "role": "assistant", "phase": "final_answer", "status": "completed",
             "content": [{"type": "output_text", "text": "Saved new final answer"}]},
            {"id": "new_input", "turn_id": "turn_followup", "type": "message",
             "role": "user", "content": [{"type": "input_text", "text": message}]},
        ]
        code, stdout, stderr = self.run_cli("send", "--message", message)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Saved new final answer\n")
        self.assertNotIn("Old turn answer", stdout)
        self.assertNotIn("Unverified streamed answer", stdout)
        paths = [request["path"] for request in self.scenario.requests]
        self.assertLess(paths.index("/v1/agents/sessions/sess_test/events?stream=true"),
                        paths.index("/v1/agents/sessions/sess_test/events"))
        self.assertIn("/v1/agents/sessions/sess_test/items?order=desc&limit=100", paths)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["last_turn_id"], "turn_followup")
        self.assertNotIn("sess_test", saved.get("pending_submissions", {}))

    def test_transient_send_failure_reuses_saved_key_on_same_message_retry(self) -> None:
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test"}), encoding="utf-8")
        self.scenario.followup_post_failures = 1
        code, _, stderr = self.run_cli("send", "--message", "Retry this question")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 503", stderr)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        pending = saved["pending_submissions"]["sess_test"]
        self.assertEqual(pending["message"], "Retry this question")
        self.assertEqual(pending["previous_turn_id"], "turn_previous")
        key = pending["idempotency_key"]
        self.assertTrue(key)
        session, turns, subscribe, first_post = self.scenario.requests
        self.assertEqual(session["method"], "GET")
        self.assertEqual(turns["method"], "GET")
        self.assertEqual(subscribe["method"], "GET")
        self.assertEqual(first_post["headers"]["Idempotency-Key"], key)

        self.scenario.followup_posted.clear()
        code, stdout, stderr = self.run_cli("send", "--message", "Retry this question")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Early final\nFollow-up reply\n")
        self.assertIn(f"Retried the saved message with Idempotency-Key: {key}", stderr)
        self.assertEqual(len(self.scenario.requests), 7)
        retry_turns, retry_subscribe, retry_post = self.scenario.requests[4:]
        self.assertEqual(retry_turns["path"],
                         "/v1/agents/sessions/sess_test/turns?order=desc&limit=1")
        self.assertEqual(retry_subscribe["path"],
                         "/v1/agents/sessions/sess_test/events?stream=true")
        self.assertEqual(retry_post["method"], "POST")
        self.assertEqual(retry_post["body"], first_post["body"])
        self.assertEqual(retry_post["headers"]["Idempotency-Key"], key)
        retained = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("sess_test", retained.get("pending_submissions", {}))
        self.assertEqual(retained["last_turn_id"], "turn_followup")

    def test_pending_message_reconciles_saved_failed_turn_without_repost(self) -> None:
        message = "A question that failed"
        pending = {"message": message, "idempotency_key": "retry-failed",
                   "previous_turn_id": "turn_previous"}
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test",
                                              "pending_submissions": {"sess_test": pending}}),
                                   encoding="utf-8")
        self.scenario.latest_turn = {"id": "turn_newer_failed", "status": "failed",
                                     "error": {"message": "Turn failed after acceptance"}}
        self.scenario.saved_items = [
            {"id": "input_failed", "turn_id": "turn_newer_failed", "type": "message",
             "role": "user", "content": [{"type": "input_text", "text": message}]},
        ]
        code, stdout, stderr = self.run_cli("send", "--message", message)
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("turn_newer_failed, which failed: Turn failed after acceptance", stderr)
        self.assertEqual([request["path"] for request in self.scenario.requests], [
            "/v1/agents/sessions/sess_test/turns?order=desc&limit=1",
            "/v1/agents/sessions/sess_test/items?order=desc&limit=100",
        ])
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))
                         ["pending_submissions"]["sess_test"], pending)

    def test_pending_message_recovers_completed_saved_turn_without_repost(self) -> None:
        message = "A question already answered"
        pending = {"message": message, "idempotency_key": "retry-completed",
                   "previous_turn_id": "turn_previous"}
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test",
                                              "pending_submissions": {"sess_test": pending}}),
                                   encoding="utf-8")
        self.scenario.latest_turn = {"id": "turn_newer_completed", "status": "completed"}
        self.scenario.saved_items = [
            {"id": "output_completed", "turn_id": "turn_newer_completed", "type": "message",
             "role": "assistant", "phase": "final_answer", "status": "completed",
             "content": [{"type": "output_text", "text": "Recovered answer"}]},
            {"id": "input_completed", "turn_id": "turn_newer_completed", "type": "message",
             "role": "user", "content": [{"type": "input_text", "text": message}]},
        ]
        code, stdout, stderr = self.run_cli("send", "--message", message)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Recovered answer\n")
        self.assertIn("Recovered completed turn turn_newer_completed from saved items", stderr)
        self.assertEqual([request["path"] for request in self.scenario.requests], [
            "/v1/agents/sessions/sess_test/turns?order=desc&limit=1",
            "/v1/agents/sessions/sess_test/items?order=desc&limit=100",
        ])
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["last_turn_id"], "turn_newer_completed")
        self.assertNotIn("sess_test", saved.get("pending_submissions", {}))

    def test_completed_saved_turn_with_only_commentary_remains_pending(self) -> None:
        message = "A question awaiting a final answer"
        pending = {"message": message, "idempotency_key": "retry-commentary",
                   "previous_turn_id": "turn_previous"}
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test",
                                              "pending_submissions": {"sess_test": pending}}),
                                   encoding="utf-8")
        self.scenario.latest_turn = {"id": "turn_commentary_only", "status": "completed"}
        self.scenario.saved_items = [
            {"id": "commentary_output", "turn_id": "turn_commentary_only", "type": "message",
             "role": "assistant", "phase": "commentary", "status": "completed",
             "content": [{"type": "output_text", "text": "Working through the question"}]},
            {"id": "commentary_input", "turn_id": "turn_commentary_only", "type": "message",
             "role": "user", "content": [{"type": "input_text", "text": message}]},
        ]
        code, stdout, stderr = self.run_cli("send", "--message", message)
        self.assertEqual(code, 2, stderr)
        self.assertEqual(stdout, "")
        self.assertIn("Pending:", stderr)
        self.assertNotIn("Recovered completed turn", stderr)
        self.assertEqual([request["path"] for request in self.scenario.requests], [
            "/v1/agents/sessions/sess_test/turns?order=desc&limit=1",
            "/v1/agents/sessions/sess_test/items?order=desc&limit=100",
        ])
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["pending_submissions"]["sess_test"], pending)
        self.assertNotIn("last_turn_id", saved)

    def test_pending_recovery_retains_saved_tool_failures_without_repost(self) -> None:
        message = "Recover my search answer"
        pending = {"message": message, "idempotency_key": "retry-tool-failure",
                   "previous_turn_id": "turn_previous"}
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test",
                                              "pending_submissions": {"sess_test": pending}}),
                                   encoding="utf-8")
        self.scenario.latest_turn = {"id": "turn_saved_failure", "status": "completed"}
        self.scenario.saved_items = [
            {"id": "saved_final", "turn_id": "turn_saved_failure", "type": "message",
             "role": "assistant", "phase": "final_answer", "status": "completed",
             "content": [{"type": "output_text", "text": "Partial search answer"}]},
            {"id": "saved_search", "turn_id": "turn_saved_failure", "type": "web_search_call",
             "status": "incomplete"},
            {"id": "saved_input", "turn_id": "turn_saved_failure", "type": "message",
             "role": "user", "content": [{"type": "input_text", "text": message}]},
        ]
        code, stdout, stderr = self.run_cli("send", "--message", message)
        self.assertEqual(code, 1, stderr)
        self.assertEqual(stdout, "Partial search answer\n")
        self.assertIn("web_search_call saved_search: status=incomplete", stderr)
        self.assertIn("treat the answer as degraded", stderr)
        self.assertFalse(any(request["method"] == "POST" for request in self.scenario.requests))
        self.assertEqual(json.loads(self.state_path.read_text(encoding="utf-8"))
                         ["pending_submissions"]["sess_test"], pending)

    def test_ambiguous_stream_recovery_retains_missed_tool_failure(self) -> None:
        message = "Search and recover saved items"
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test"}), encoding="utf-8")
        self.scenario.followup_events = [
            {"type": "agent.session.turn.item.done", "turn_id": None, "item": {
                "id": "saved_search", "type": "web_search_call", "status": "failed"}},
            {"type": "agent.session.turn.started", "turn": {"id": "turn_followup"}},
            {"type": "agent.session.turn.output_text.done", "turn_id": "turn_followup",
             "item_id": "stream_final", "text": "Stream answer"},
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_followup"}},
        ]
        self.scenario.saved_items = [
            {"id": "saved_final", "turn_id": "turn_followup", "type": "message",
             "role": "assistant", "phase": "final_answer", "status": "completed",
             "content": [{"type": "output_text", "text": "Saved partial answer"}]},
            {"id": "saved_search", "turn_id": "turn_followup", "type": "web_search_call",
             "status": "completed", "error": {"message": "Search backend failed"}},
            {"id": "saved_input", "turn_id": "turn_followup", "type": "message",
             "role": "user", "content": [{"type": "input_text", "text": message}]},
        ]
        code, stdout, stderr = self.run_cli("send", "--message", message)
        self.assertEqual(code, 1, stderr)
        self.assertEqual(stdout, "Saved partial answer\n")
        self.assertIn("Search backend failed", stderr)
        self.assertIn("treat the answer as degraded", stderr)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["last_turn_id"], "turn_followup")
        self.assertIn("sess_test", saved["pending_submissions"])

    def test_completed_saved_turn_with_in_progress_final_item_remains_pending(self) -> None:
        message = "A question whose final item is still in progress"
        pending = {"message": message, "idempotency_key": "retry-in-progress",
                   "previous_turn_id": "turn_previous"}
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test",
                                              "pending_submissions": {"sess_test": pending}}),
                                   encoding="utf-8")
        self.scenario.latest_turn = {"id": "turn_in_progress_item", "status": "completed"}
        self.scenario.saved_items = [
            {"id": "unfinished_output", "turn_id": "turn_in_progress_item", "type": "message",
             "role": "assistant", "phase": "final_answer", "status": "in_progress",
             "content": [{"type": "output_text", "text": "Unfinished draft"}]},
            {"id": "matching_input", "turn_id": "turn_in_progress_item", "type": "message",
             "role": "user", "content": [{"type": "input_text", "text": message}]},
        ]
        code, stdout, stderr = self.run_cli("send", "--message", message)
        self.assertEqual(code, 2, stderr)
        self.assertEqual(stdout, "")
        self.assertIn("Pending:", stderr)
        self.assertNotIn("Recovered completed turn", stderr)
        self.assertEqual([request["path"] for request in self.scenario.requests], [
            "/v1/agents/sessions/sess_test/turns?order=desc&limit=1",
            "/v1/agents/sessions/sess_test/items?order=desc&limit=100",
        ])
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["pending_submissions"]["sess_test"], pending)
        self.assertNotIn("last_turn_id", saved)

    def test_status_shows_latest_turn_and_requests_latest_items(self) -> None:
        self.state_path.write_text(json.dumps({"project_id": app.PROJECT_ID,
                                              "session_id": "sess_test"}), encoding="utf-8")
        code, stdout, stderr = self.run_cli("status", "--items")
        self.assertEqual(code, 0, stderr)
        self.assertIn('"latest_turn": {', stdout)
        self.assertIn('"id": "turn_previous"', stdout)
        self.assertIn('"id": "latest_item"', stdout)
        self.assertEqual([request["path"] for request in self.scenario.requests], [
            "/v1/agents/sessions/sess_test",
            "/v1/agents/sessions/sess_test/turns?order=desc&limit=1",
            "/v1/agents/sessions/sess_test/items?order=desc&limit=100",
        ])

    def test_corrected_done_text_replaces_provisional_delta(self) -> None:
        self.scenario.start_events = [
            {"type": "agent.session.created", "session": {"id": "sess_test"}},
            {"type": "agent.session.turn.output_text.delta", "turn_id": "turn_test",
             "item_id": "msg_test", "output_index": 0, "content_index": 0,
             "delta": "Provisional answer"},
            {"type": "agent.session.turn.output_text.done", "turn_id": "turn_test",
             "item_id": "msg_test", "output_index": 0, "content_index": 0,
             "text": "Corrected final answer"},
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_test"}},
        ]
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Corrected final answer\n")
        self.assertIn("Provisional answer", stderr)

    def test_api_error_is_reported_without_starting_session(self) -> None:
        self.scenario.agent_error = True
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("HTTP 401", stderr)
        self.assertIn("invalid_api_key: Test key rejected", stderr)
        self.assertEqual([request["path"] for request in self.scenario.requests],
                         [f"/v1/agents/{app.AGENT_ID}"])
        self.assertFalse(self.state_path.exists())

    def test_unexpected_saved_tool_blocks_session_before_initial_message(self) -> None:
        self.scenario.agent_tools = [{"type": "web_search"}, {"type": "function"}]
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("only the web_search tool", stderr)
        self.assertEqual([request["path"] for request in self.scenario.requests],
                         [f"/v1/agents/{app.AGENT_ID}"])
        self.assertFalse(self.state_path.exists())

    def test_failed_root_turn_reports_error_and_retains_session_id(self) -> None:
        self.scenario.start_events = [
            {"type": "agent.session.created", "session": {"id": "sess_test"}},
            {"type": "agent.session.turn.failed", "turn": {
                "id": "turn_test", "error": {"message": "Search service unavailable"}}},
        ]
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Search service unavailable", stderr)
        self.assertEqual(json.loads(self.state_path.read_text())["session_id"], "sess_test")

    def test_incomplete_web_search_marks_completed_turn_degraded(self) -> None:
        self.scenario.start_events = [
            {"type": "agent.session.created", "session": {"id": "sess_test"}},
            {"type": "agent.session.turn.item.done", "item": {
                "id": "search_1", "type": "web_search_call", "status": "incomplete"}},
            {"type": "agent.session.turn.output_text.done", "item_id": "msg_test",
             "text": "Here is what I could find."},
            {"type": "agent.session.turn.completed", "turn": {"id": "turn_test"}},
        ]
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertIn("Here is what I could find.", stdout)
        self.assertIn("treat the answer as degraded", stderr)
        self.assertIn("web_search_call search_1: status=incomplete", stderr)
        self.assertEqual(json.loads(self.state_path.read_text())["last_turn_id"], "turn_test")

    def test_stream_error_is_reported_with_session_id_retained(self) -> None:
        self.scenario.start_events = [
            {"type": "agent.session.created", "session": {"id": "sess_test"}},
            {"type": "error", "error": {"message": "Test stream failure"}},
        ]
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Agent stream error: Test stream failure", stderr)
        self.assertEqual(json.loads(self.state_path.read_text())["session_id"], "sess_test")

    def test_required_action_is_retrieved_and_reported(self) -> None:
        self.scenario.start_events = [
            {"type": "agent.session.created", "session": {"id": "sess_test"}},
            {"type": "agent.session.requires_action", "session_id": "sess_test"},
        ]
        code, _, stderr = self.run_cli("start")
        self.assertEqual(code, 1)
        self.assertIn('"id": "action_test"', stderr)
        self.assertIn("needs an application action", stderr)
        self.assertIn("/v1/agents/sessions/sess_test",
                      [request["path"] for request in self.scenario.requests])
        self.assertEqual(json.loads(self.state_path.read_text())["session_id"], "sess_test")


if __name__ == "__main__":
    unittest.main()
