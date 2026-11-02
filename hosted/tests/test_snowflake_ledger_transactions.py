"""Offline transaction tests; no provider dispatch or custody admission."""

from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid

from snowflake_dispatch_ledger import DispatchHeld, reserve_once


class CommitAcknowledgementLost(sqlite3.Connection):
    def commit(self):
        super().commit()
        raise OSError("synthetic acknowledgement lost after local commit")

    def execute(self, sql, parameters=()):
        result = super().execute(sql, parameters)
        if sql == "COMMIT":
            raise OSError("synthetic acknowledgement lost after local commit")
        return result


class LedgerTransactionTests(unittest.TestCase):
    def modes(self):
        yield {}
        yield {"isolation_level": None}
        if hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"):
            yield {"autocommit": True}

    def reserve(self, connection):
        return reserve_once(connection, str(uuid.uuid4()), "a" * 64, "b" * 64)

    def test_returned_reservation_is_committed_before_close(self):
        for mode in self.modes():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "hold.sqlite"
                connection = sqlite3.connect(path, **mode)
                try:
                    receipt = self.reserve(connection)
                    self.assertFalse(connection.in_transaction)
                    # Independent readback while the writer is still open.
                    observer = sqlite3.connect(path)
                    try:
                        self.assertEqual(observer.execute(
                            "SELECT receipt_sha256 FROM studio_dispatch_hold"
                        ).fetchall(), [(receipt["receipt_sha256"],)])
                    finally:
                        observer.close()
                finally:
                    connection.close()
                reopened = sqlite3.connect(path, **mode)
                try:
                    with self.assertRaises(DispatchHeld):
                        self.reserve(reopened)
                    self.assertFalse(reopened.in_transaction)
                finally:
                    reopened.close()

    def test_commit_failure_rolls_back_without_returning_a_receipt(self):
        for mode in self.modes():
            with self.subTest(mode=mode):
                connection = sqlite3.connect(":memory:", **mode)
                try:
                    # Real SQLite failure injection before commit, not a mock.
                    def deny_commit(action, operation, *unused):
                        if action == sqlite3.SQLITE_TRANSACTION and operation == "COMMIT":
                            return sqlite3.SQLITE_DENY
                        return sqlite3.SQLITE_OK

                    connection.set_authorizer(deny_commit)
                    with self.assertRaises(sqlite3.DatabaseError):
                        self.reserve(connection)
                    self.assertFalse(connection.in_transaction)
                    self.assertEqual(connection.execute(
                        "SELECT name FROM sqlite_master WHERE name='studio_dispatch_hold'"
                    ).fetchall(), [])
                finally:
                    connection.close()

    def test_lost_commit_acknowledgement_never_releases_the_hold(self):
        for mode in self.modes():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "hold.sqlite"
                connection = sqlite3.connect(path, factory=CommitAcknowledgementLost, **mode)
                try:
                    with self.assertRaisesRegex(OSError, "acknowledgement lost"):
                        self.reserve(connection)
                    self.assertFalse(connection.in_transaction)
                finally:
                    connection.close()
                reopened = sqlite3.connect(path, **mode)
                try:
                    original = reopened.execute("SELECT * FROM studio_dispatch_hold").fetchall()
                    self.assertEqual(len(original), 1)
                    with self.assertRaises(DispatchHeld):
                        self.reserve(reopened)
                    self.assertFalse(reopened.in_transaction)
                    self.assertEqual(reopened.execute(
                        "SELECT * FROM studio_dispatch_hold").fetchall(), original)
                finally:
                    reopened.close()


if __name__ == "__main__":
    unittest.main()
