"""Exercise the New agent create/start/send flow over local HTTP with real curl."""

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
import new_agent  # noqa: E402


class NewAgentIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        if not (shutil.which("curl.exe") or shutil.which("curl")):
            self.skipTest("curl executable is unavailable")
        self.requests: list[dict[str, object]] = []
        self.lock = threading.Lock()
        self.followup_posted = threading.Event()
        self.agent = json.loads(new_agent.DEFINITION_PATH.read_text(encoding="utf-8"))
        self.agent["id"] = "agent_new_test"
        self.list_existing = False
        self.listing_pages: dict[str, dict[str, object]] = {}
        scenario = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def respond(self, status: int, body: dict[str, object]) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def stream(self, events: list[dict[str, object]], wait_for_post: bool = False) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                if wait_for_post and not scenario.followup_posted.wait(timeout=5):
                    return
                for event in events:
                    self.wfile.write(b"data: " + json.dumps(event).encode("utf-8") + b"\n\n")
                self.wfile.flush()

            def dispatch(self, method: str) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(size) if size else b""
                with scenario.lock:
                    scenario.requests.append({
                        "method": method, "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(raw) if raw else None,
                    })
                if method == "GET" and self.path.startswith("/v1/agents?"):
                    self.respond(200, scenario.listing_pages.get(self.path, {
                        "data": [scenario.agent] if scenario.list_existing else [], "has_more": False}))
                elif (method, self.path) == ("POST", "/v1/agents"):
                    self.respond(201, scenario.agent)
                elif (method, self.path) == ("GET", "/v1/agents/agent_new_test"):
                    self.respond(200, scenario.agent)
                elif (method, self.path) == ("POST", "/v1/agents/sessions"):
                    self.stream([
                        {"type": "agent.session.created", "session": {"id": "sess_new_test"}},
                        {"type": "agent.session.turn.item.done", "item": {
                            "id": "tool_new", "type": "web_search_call", "status": "completed"}},
                        {"type": "agent.session.turn.output_text.done", "turn_id": "turn_new",
                         "item_id": "reply_new", "text": "Ready to research."},
                        {"type": "agent.session.turn.completed", "turn": {"id": "turn_new"}},
                    ])
                elif (method, self.path) == ("GET", "/v1/agents/sessions/sess_new_test"):
                    self.respond(200, {"id": "sess_new_test", "status": "idle"})
                elif (method, self.path) == ("GET", "/v1/agents/sessions/sess_new_test/turns?order=desc&limit=1"):
                    self.respond(200, {"data": [{"id": "turn_new"}]})
                elif (method, self.path) == ("GET", "/v1/agents/sessions/sess_new_test/events?stream=true"):
                    self.stream([
                        {"type": "agent.session.turn.started", "turn": {"id": "turn_followup"}},
                        {"type": "agent.session.turn.output_text.done", "turn_id": "turn_followup",
                         "item_id": "reply_followup", "text": "I can research sources."},
                        {"type": "agent.session.turn.completed", "turn": {"id": "turn_followup"}},
                    ], wait_for_post=True)
                elif (method, self.path) == ("POST", "/v1/agents/sessions/sess_new_test/events"):
                    scenario.followup_posted.set()
                    self.respond(202, {"data": []})
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
        self.tempdir = tempfile.TemporaryDirectory(prefix="new-agent-test-")
        self.addCleanup(self.tempdir.cleanup)
        self.state_path = Path(self.tempdir.name) / "new-agent-state.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(app, "BASE_URL", f"http://127.0.0.1:{self.server.server_port}/v1").start()
        mock.patch.object(new_agent, "STATE_PATH", self.state_path).start()
        mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-only"}).start()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = new_agent.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_creation_uses_returned_id_and_followup_reuses_session(self) -> None:
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Ready to research.\n")
        self.assertIn("Created reusable agent: New agent (agent_new_test)", stderr)
        self.assertIn("Session ID: sess_new_test", stderr)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["agent_id"], "agent_new_test")
        self.assertEqual(state["session_id"], "sess_new_test")
        self.assertNotIn("OPENAI_API_KEY", state)
        create = next(request for request in self.requests if request["path"] == "/v1/agents")
        session = next(request for request in self.requests if request["path"] == "/v1/agents/sessions")
        self.assertEqual(create["body"], json.loads(new_agent.DEFINITION_PATH.read_text(encoding="utf-8")))
        self.assertEqual(create["headers"]["OpenAI-Project"], app.PROJECT_ID)
        self.assertEqual(session["body"], {"agent_id": "agent_new_test", "environment": {"type": "none"},
                                           "input": new_agent.DEFAULT_MESSAGE, "stream": True})

        code, stdout, stderr = self.run_cli("send", "--message", "Follow up")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "I can research sources.\n")
        self.assertEqual([request["path"] for request in self.requests].count("/v1/agents"), 1)
        post = self.requests[-1]
        self.assertEqual(post["path"], "/v1/agents/sessions/sess_new_test/events")
        self.assertIn("Idempotency-Key", post["headers"])
        self.assertEqual(json.loads(self.state_path.read_text())["last_turn_id"], "turn_followup")

    def test_later_start_verifies_agent_without_creating_duplicate(self) -> None:
        self.assertEqual(self.run_cli("start")[0], 0)
        code, _, stderr = self.run_cli("start", "--message", "A second session")
        self.assertEqual(code, 0, stderr)
        self.assertIn("Reusing saved agent", stderr)
        self.assertEqual([request["path"] for request in self.requests].count("/v1/agents"), 1)

    def test_missing_state_adopts_matching_saved_agent(self) -> None:
        self.list_existing = True
        code, stdout, stderr = self.run_cli("start")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout, "Ready to research.\n")
        self.assertIn("Recovered matching saved agent", stderr)
        self.assertFalse(any(request["method"] == "POST" and request["path"] == "/v1/agents"
                             for request in self.requests))
        self.assertEqual(json.loads(self.state_path.read_text())["agent_id"], "agent_new_test")

    def test_malformed_listing_flags_never_create_an_agent_or_write_state(self) -> None:
        initial_path = "/v1/agents?limit=100&order=desc"
        for label, page in [("missing", {"data": []}),
                            *[(repr(value), {"data": [], "has_more": value})
                              for value in (None, 0, 1, "", "false", "true")]]:
            with self.subTest(flag=label):
                self.state_path.unlink(missing_ok=True)
                self.requests.clear()
                self.listing_pages = {initial_path: page}
                code, _, stderr = self.run_cli("create")
                self.assertEqual(code, 1, stderr)
                self.assertIn("invalid page", stderr)
                self.assertFalse(self.state_path.exists())
                self.assertEqual([(request["method"], request["path"]) for request in self.requests],
                                 [("GET", initial_path)])

    def test_malformed_or_repeated_listing_cursor_never_creates_an_agent(self) -> None:
        initial_path = "/v1/agents?limit=100&order=desc"
        for cursor in (None, "", "   ", 1):
            with self.subTest(cursor=cursor):
                self.requests.clear()
                self.listing_pages = {initial_path: {"data": [], "has_more": True, "last_id": cursor}}
                code, _, stderr = self.run_cli("create")
                self.assertEqual(code, 1, stderr)
                self.assertIn("pagination is invalid", stderr)
                self.assertFalse(self.state_path.exists())
                self.assertEqual(len(self.requests), 1)
        self.requests.clear()
        self.listing_pages = {
            initial_path: {"data": [], "has_more": True, "last_id": "agent_cursor_a"},
            initial_path + "&after=agent_cursor_a": {"data": [], "has_more": True, "last_id": "agent_cursor_b"},
            initial_path + "&after=agent_cursor_b": {"data": [], "has_more": True, "last_id": "agent_cursor_a"},
        }
        code, _, stderr = self.run_cli("create")
        self.assertEqual(code, 1, stderr)
        self.assertIn("pagination is invalid", stderr)
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all(request["method"] == "GET" for request in self.requests))
        self.assertFalse(self.state_path.exists())

    def test_message_file_preserves_quotes_unicode_backslashes_and_line_endings(self) -> None:
        message = 'Research "quoted value" with caf\u00e9 and C:\\research\\\r\nSecond line\nLast line.'
        message_file = Path(self.tempdir.name) / "message with spaces.txt"
        message_file.write_bytes(message.encode("utf-8"))
        code, _, stderr = self.run_cli("start", "--message-file", str(message_file))
        self.assertEqual(code, 0, stderr)
        session = next(request for request in self.requests if request["path"] == "/v1/agents/sessions")
        self.assertEqual(session["body"]["input"], message)
        self.assertEqual(session["body"]["environment"], {"type": "none"})
        self.assertEqual(session["body"]["agent_id"], "agent_new_test")
        code, _, stderr = self.run_cli("send", "--message-file", str(message_file))
        self.assertEqual(code, 0, stderr)
        post = next(request for request in self.requests
                    if (request["method"], request["path"]) ==
                    ("POST", "/v1/agents/sessions/sess_new_test/events"))
        self.assertEqual(post["body"]["events"][0]["input"][0]["content"][0]["text"], message)
        self.assertIn("Idempotency-Key", post["headers"])
        self.assertFalse(json.loads(self.state_path.read_bytes()).get("pending_submissions"))

    def test_invalid_message_file_stops_before_http_or_state_write(self) -> None:
        cases = [("missing.txt", None, "Cannot read message file"),
                 ("invalid.txt", b"\xff", "must contain UTF-8 text"),
                 ("empty.txt", b" \r\n\t", "Message cannot be empty")]
        for name, content, error in cases:
            with self.subTest(file=name):
                message_file = Path(self.tempdir.name) / name
                if content is not None:
                    message_file.write_bytes(content)
                code, _, stderr = self.run_cli("start", "--message-file", str(message_file))
                self.assertEqual(code, 1, stderr)
                self.assertIn(error, stderr)
                self.assertEqual(self.requests, [])
                self.assertFalse(self.state_path.exists())

    def test_message_and_message_file_are_mutually_exclusive(self) -> None:
        for command in ("start", "send"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as outcome:
                    self.run_cli(command, "--message", "inline", "--message-file", "not-read.txt")
                self.assertEqual(outcome.exception.code, 2)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.state_path.exists())


if __name__ == "__main__":
    unittest.main()
