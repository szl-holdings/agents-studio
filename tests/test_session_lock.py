"""Verify exclusive ownership and durable unknown-start behavior offline."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import new_agent
from session_lock import SessionBusy, exclusive_lock


class SessionLockTests(unittest.TestCase):
    def test_second_process_cannot_acquire_lock_and_release_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.lock"
            script = "from pathlib import Path; import sys; from session_lock import exclusive_lock;\nwith exclusive_lock(Path(sys.argv[1])): pass"
            with exclusive_lock(path):
                result = subprocess.run([sys.executable, "-B", "-c", script, str(path)], cwd=Path(new_agent.__file__).parent, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SessionBusy", result.stderr)
                with self.assertRaises(SessionBusy):
                    with exclusive_lock(path):
                        pass
            with exclusive_lock(path):
                pass

    def test_exception_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.lock"
            with self.assertRaises(ValueError):
                with exclusive_lock(path):
                    raise ValueError("test")
            with exclusive_lock(path):
                pass

    def test_unknown_start_marker_blocks_second_post(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with mock.patch.object(new_agent, "STATE_PATH", state_path), mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-only"}), mock.patch.object(new_agent, "_ensure_agent", return_value="agent_test") as ensure, mock.patch.object(app, "_open_stream", side_effect=app.AppError("lost response")) as stream:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(new_agent.main(["start", "--message", "One start"]), 1)
                    saved = json.loads(state_path.read_text())
                    self.assertIn("pending_start", saved)
                    self.assertEqual(new_agent.main(["start", "--message", "One start"]), 2)
                self.assertEqual(ensure.call_count, 1)
                self.assertEqual(stream.call_count, 1)
                self.assertEqual(json.loads(state_path.read_text())["pending_start"], saved["pending_start"])

    def test_cli_busy_lock_makes_no_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            with mock.patch.object(new_agent, "STATE_PATH", state_path), mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-only"}), mock.patch.object(app, "_api_json") as api:
                with exclusive_lock(state_path.with_suffix(".lock")), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(new_agent.main(["create"]), 1)
                api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
