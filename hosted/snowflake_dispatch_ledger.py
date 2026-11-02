"""LOCAL PROTOTYPE: reserve one Snowflake dispatch, never release or resend.

This deliberately does not send requests or reconcile provider evidence. Its
storage location, durability, custody, and receipt signing are NOT VERIFIED.
Do not use it to authorize a production export.
"""

import hashlib
import json
import re
import sqlite3
import uuid


class DispatchHeld(Exception):
    pass


def reserve_once(connection, request_id, target_sha256, payload_sha256):
    """Atomically retain one target/payload/request identity and an unsigned receipt.

    A later reservation fails closed even after a crash or timeout. This
    prototype intentionally has no clear or retry function.
    """
    try:
        request = uuid.UUID(request_id)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("invalid_request_id") from None
    if request.version != 4 or str(request) != request_id:
        raise ValueError("invalid_request_id")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
           for value in (target_sha256, payload_sha256)):
        raise ValueError("invalid_digest")
    if not isinstance(connection, sqlite3.Connection) or connection.in_transaction:
        raise ValueError("invalid_connection")

    record = {"event": "dispatch_reserved", "payload_sha256": payload_sha256,
              "request_id": request_id, "target_sha256": target_sha256,
              "evidence_class": "DECLARED", "signature": "UNSIGNED"}
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
    receipt_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("CREATE TABLE IF NOT EXISTS studio_dispatch_hold "
                           "(slot INTEGER PRIMARY KEY CHECK(slot = 1), "
                           "request_id TEXT NOT NULL, target_sha256 TEXT NOT NULL, "
                           "payload_sha256 TEXT NOT NULL, receipt_json TEXT NOT NULL, "
                           "receipt_sha256 TEXT NOT NULL)")
        if connection.execute("SELECT 1 FROM studio_dispatch_hold LIMIT 1").fetchone():
            raise DispatchHeld("dispatch_already_held")
        connection.execute("INSERT INTO studio_dispatch_hold VALUES (1, ?, ?, ?, ?, ?)",
                           (request_id, target_sha256, payload_sha256, encoded, receipt_sha256))
        # Match the explicit BEGIN with SQL transaction control. Python 3.12's
        # Connection.commit()/rollback() are no-ops with autocommit=True, even
        # when BEGIN IMMEDIATE opened a transaction. Return a receipt only after
        # this reservation has actually committed.
        connection.execute("COMMIT")
    except Exception:
        # A lost acknowledgement can follow a successful commit. In that case
        # retain the hold; there is no active transaction left to roll back.
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return {"receipt_sha256": receipt_sha256, "receipt": record}
