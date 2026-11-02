import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import snowflake_operator


class OperatorTests(unittest.TestCase):
    def test_operator_blocks_before_reading_credentials_or_dispatching(self):
        # Installed connectors and configured credentials are not admission.
        # No process environment is inspected before reporting the hold.
        connector = mock.Mock()
        modules = {"snowflake": mock.Mock(connector=connector),
                   "snowflake.connector": connector}
        with mock.patch.dict(sys.modules, modules):
            with mock.patch.object(os, "environ") as environ:
                environ.get.side_effect = AssertionError("credential read")
                environ.__getitem__.side_effect = AssertionError("credential read")
                environ.__iter__.side_effect = AssertionError("credential read")
                with mock.patch("urllib.request.OpenerDirector.open") as http:
                    with mock.patch("tempfile.TemporaryDirectory") as key_directory:
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            self.assertEqual(snowflake_operator.run(), 1)
        self.assertEqual(output.getvalue(), "snowflake_transport_not_verified\n")
        connector.connect.assert_not_called()
        http.assert_not_called()
        key_directory.assert_not_called()

    def test_fresh_process_restarts_remain_blocked_despite_enable_flags(self):
        # A hostile stand-in proves the installed provider is never imported.
        # Each attempt is a fresh interpreter, as with scheduler reruns.
        operator = Path(snowflake_operator.__file__).resolve()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "snowflake"
            package.mkdir()
            (package / "__init__.py").write_text(
                "raise AssertionError('unverified connector imported')\n",
                encoding="utf-8")
            evidence = root / "prior-uncertain-dispatch.json"
            original = b'{"state":"UNKNOWN","request_id":"synthetic-prior-request"}\n'
            evidence.write_bytes(original)
            for disabled in ("0", "1"):
                env = {**os.environ, "PYTHONPATH": str(root),
                       "SZL_METRICS_EXPORT_ENABLED": "1",
                       "STUDIO_SNOWFLAKE_SERVICE_USER_DISABLED": disabled,
                       "STUDIO_SNOWFLAKE_PRIVATE_KEY_B64": "c3ludGhldGljLW9ubHk=",
                       "STUDIO_METRICS_SECRET": "synthetic-secret-never-used-live"}
                for attempt in range(2):
                    with self.subTest(disabled=disabled, restart=attempt):
                        result = subprocess.run(
                            [sys.executable, "-B", str(operator)], cwd=root,
                            env=env, text=True, capture_output=True, timeout=10)
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(result.stdout, "snowflake_transport_not_verified\n")
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(evidence.read_bytes(), original)
            self.assertEqual({path.name for path in root.iterdir()},
                             {"snowflake", "prior-uncertain-dispatch.json"})


if __name__ == "__main__":
    unittest.main()
