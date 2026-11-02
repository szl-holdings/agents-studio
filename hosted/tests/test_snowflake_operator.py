import io
import sys
import unittest
from unittest import mock

import snowflake_operator
from snowflake_metrics_export import ExportError


class OperatorTests(unittest.TestCase):
    def test_invalid_key_material_is_a_configuration_failure(self):
        connector = mock.Mock()
        modules = {"snowflake": mock.Mock(), "snowflake.connector": connector}
        environ = {"STUDIO_SNOWFLAKE_PRIVATE_KEY_B64": ""}
        with mock.patch.dict(sys.modules, modules):
            with mock.patch.dict("os.environ", environ, clear=False):
                with mock.patch("sys.stdout") as stdout:
                    code = snowflake_operator.run()
        self.assertEqual(code, 1)
        printed = "".join(call.args[0] for call in stdout.write.call_args_list)
        self.assertIn("snowflake_configuration_invalid", printed)
        connector.connect.assert_not_called()

    def test_writer_failure_prints_only_redacted_diagnostics_and_cleans_key(self):
        connector = mock.Mock()
        modules = {"snowflake": mock.Mock(), "snowflake.connector": connector}
        directory = mock.Mock()
        error = ExportError("snowflake_write_uncertain", phase="execute", errno=604, sqlstate="57014")
        stdout = io.StringIO()
        with mock.patch.dict(sys.modules, modules):
            with mock.patch.object(snowflake_operator, "_private_key_file",
                                   return_value=("/synthetic/private-key.p8", directory)):
                with mock.patch.object(snowflake_operator, "export_once", side_effect=error) as export:
                    with mock.patch("sys.stdout", stdout):
                        self.assertEqual(snowflake_operator.run(), 1)
        self.assertEqual(stdout.getvalue(), 'snowflake_write_uncertain\n'
                         'snowflake_diagnostic={"errno":604,"phase":"execute","sqlstate":"57014"}\n')
        export.assert_called_once()
        directory.cleanup.assert_called_once()

    def test_configuration_failures_do_not_emit_provider_diagnostics(self):
        connector = mock.Mock()
        modules = {"snowflake": mock.Mock(), "snowflake.connector": connector}
        stdout = io.StringIO()
        with mock.patch.dict(sys.modules, modules):
            with mock.patch.object(snowflake_operator, "_private_key_file",
                                   side_effect=ExportError("snowflake_configuration_invalid")):
                with mock.patch("sys.stdout", stdout):
                    self.assertEqual(snowflake_operator.run(), 1)
        self.assertEqual(stdout.getvalue(), "snowflake_configuration_invalid\n")


if __name__ == "__main__":
    unittest.main()
