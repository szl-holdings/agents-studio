"""One-shot operator entrypoint for the external Snowflake metrics writer.

This process is intentionally outside the Hugging Face Space. It reads only
owner-free aggregate metrics from the dedicated Worker and uses a Snowflake
keypair held by the scheduler's secret store. It never logs secret values or
conversation data. A scheduler must provide one-writer concurrency and retain
UNKNOWN outcomes for operator reconciliation.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path
import tempfile

from snowflake_metrics_export import ExportError, export_once


def _private_key_file() -> tuple[str, tempfile.TemporaryDirectory[str]]:
    encoded = os.environ.get("STUDIO_SNOWFLAKE_PRIVATE_KEY_B64", "")
    if not encoded or "\n" in encoded or "\r" in encoded:
        raise ExportError("snowflake_configuration_invalid")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ExportError("snowflake_configuration_invalid") from None
    if not payload or len(payload) > 16 * 1024:
        raise ExportError("snowflake_configuration_invalid")
    directory = tempfile.TemporaryDirectory(prefix="szl-snowflake-key-")
    path = Path(directory.name) / "private_key.p8"
    path.write_bytes(payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return str(path), directory


def run() -> int:
    try:
        import snowflake.connector
    except ImportError:
        print("snowflake_connector_unavailable")
        return 1

    directory = None
    try:
        key_path, directory = _private_key_file()
        environ = dict(os.environ)
        environ["STUDIO_SNOWFLAKE_PRIVATE_KEY_FILE"] = key_path
        environ.setdefault("STUDIO_SNOWFLAKE_USER", "SZL_STUDIO_METRICS_SERVICE")
        environ.setdefault("STUDIO_SNOWFLAKE_ROLE", "SZL_STUDIO_METRICS_WRITER")
        environ["STUDIO_SNOWFLAKE_SERVICE_USER_DISABLED"] = "0"
        count = export_once(environ, connect=snowflake.connector.connect)
    except ExportError as error:
        print(str(error))
        if error.diagnostic is not None:
            print("snowflake_diagnostic=" + json.dumps(error.diagnostic, sort_keys=True,
                                                       separators=(",", ":")))
        return 1
    finally:
        if directory is not None:
            directory.cleanup()
    print(f"aggregate_days_exported={count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
