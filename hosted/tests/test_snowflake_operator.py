import sys
import unittest
from unittest import mock

import snowflake_operator


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


if __name__ == "__main__":
    unittest.main()
