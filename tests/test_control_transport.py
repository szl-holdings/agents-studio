"""Real CLI subprocess, curl, pipe, and local HTTP cancellation integration."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

import app
import new_agent


class ControlTransportTests(unittest.TestCase):
    def exercise(self, action):
        input_seen, cancelled = threading.Event(), threading.Event()
        requests = []
        definition, digest = new_agent._definition()
        agent = dict(definition, id="agent_local_control")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def respond(self, data, status=200):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def stream(self, initial):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                if not initial and not input_seen.wait(10):
                    return
                events = []
                if initial:
                    events.append({"type": "agent.session.created", "session": {"id": "sess_control"}})
                for event_type, status in (("agent.session.turn.created", "queued"), ("agent.session.turn.in_progress", "in_progress")):
                    events.append({"type": event_type, "session_id": "sess_control", "turn": {
                        "id": "turn_control", "object": "agent.session.turn", "session_id": "sess_control",
                        "agent_id": "agent_local_control", "subagent_id": None, "status": status}})
                for event in events:
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
                if cancelled.wait(10):
                    event = {"type": "agent.session.turn.cancelled", "turn": {"id": "turn_control"}}
                    self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                    self.wfile.flush()

            def do_GET(self):
                requests.append(("GET", self.path, None))
                if self.path == "/v1/agents/agent_local_control":
                    self.respond(agent)
                elif self.path == "/v1/agents/sessions/sess_control":
                    self.respond({"id": "sess_control", "status": "idle"})
                elif self.path.endswith("/turns?order=desc&limit=1"):
                    current = action == "start" or input_seen.is_set()
                    self.respond({"data": [{"id": "turn_control" if current else "turn_previous", "status": "cancelled" if cancelled.is_set() else "in_progress" if current else "completed"}]})
                elif self.path.endswith("/events?stream=true"):
                    self.stream(False)
                elif self.path.endswith("/items?order=desc&limit=100"):
                    self.respond({"data": [{"id": "input_control", "type": "message", "role": "user", "turn_id": "turn_control", "content": [{"type": "input_text", "text": "Question"}]}], "has_more": False})
                else:
                    self.respond({"error": {"message": "Unexpected route"}}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(("POST", self.path, body))
                if self.path == "/v1/agents/sessions":
                    self.stream(True)
                elif self.path.endswith("/events"):
                    if body["events"][0]["type"] == "agent.session.input.cancel":
                        cancelled.set()
                    else:
                        input_seen.set()
                    self.respond({"data": []}, 202)
                else:
                    self.respond({"error": {"message": "Unexpected route"}}, 404)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server_thread.join, 3)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix="control-transport-") as directory:
            state_path = Path(directory) / "state.json"
            state = {"project_id": app.PROJECT_ID, "agent_id": agent["id"], "definition_sha256": digest}
            if action == "send":
                state.update(session_id="sess_control", last_turn_id="turn_previous")
            state_path.write_text(json.dumps(state))
            code = "import sys; from pathlib import Path; import app,new_agent; app.BASE_URL=sys.argv[1]; new_agent.STATE_PATH=Path(sys.argv[2]); raise SystemExit(new_agent.main([sys.argv[3], '--message', 'Question', '--raw-events', '--studio-control']))"
            process = subprocess.Popen([sys.executable, "-B", "-c", code, f"http://127.0.0.1:{server.server_port}/v1", str(state_path), action], cwd=new_agent.ROOT,
                                       env=dict(os.environ, OPENAI_API_KEY="sk-local-control-test", PYTHONIOENCODING="utf-8"),
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, encoding="utf-8")
            lines = []
            events = queue.Queue()
            def read():
                for line in process.stderr:
                    lines.append(line)
                    try:
                        events.put(json.loads(line))
                    except ValueError:
                        pass
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                while True:
                    event = events.get(timeout=10)
                    if event.get("type") == "agent.session.turn.in_progress":
                        break
                process.stdin.write(json.dumps({"action": "cancel", "session_id": "sess_control", "turn_id": "turn_control"}) + "\n")
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=15), 1)
                reader.join(3)
                self.assertFalse(reader.is_alive())
                self.assertNotIn("Fatal Python error", "".join(lines))
                controls = [json.loads(line) for line in lines if line.startswith('{"type": "studio.cancel.status"')]
                self.assertTrue(any(event["status"] == "confirmed" for event in controls))
                cancel_posts = [body for method, path, body in requests if method == "POST" and isinstance(body, dict) and body.get("events", [{}])[0].get("type") == "agent.session.input.cancel"]
                self.assertEqual(cancel_posts, [{"events": [{"type": "agent.session.input.cancel"}]}])
                saved = json.loads(state_path.read_text())
                self.assertNotIn("pending_start", saved)
                if action == "send":
                    self.assertFalse(saved["pending_submissions"])
                    self.assertEqual(next(iter(saved["resolved_submissions"].values()))["status"], "cancelled")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
                process.stdin.close()
                process.stdout.close()
                process.stderr.close()

    def test_start_cancel_through_real_child_control_pipe(self):
        self.exercise("start")

    def test_followup_cancel_resolves_new_pending_record_under_lock(self):
        self.exercise("send")


if __name__ == "__main__":
    unittest.main()
