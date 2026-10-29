import json
import sqlite3
import unittest
import uuid

from snowflake_dispatch_ledger import DispatchHeld, reserve_once


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.request_id = str(uuid.uuid4())

    def test_reservation_stores_identity_and_unsigned_receipt(self):
        result = reserve_once(self.db, self.request_id, "a" * 64, "b" * 64)
        row = self.db.execute("SELECT request_id, receipt_json, receipt_sha256 "
                              "FROM studio_dispatch_hold").fetchone()
        self.assertEqual(row[0], self.request_id)
        self.assertEqual(json.loads(row[1]), result["receipt"])
        self.assertEqual(row[2], result["receipt_sha256"])
        self.assertEqual(result["receipt"]["signature"], "UNSIGNED")

    def test_duplicate_and_distinct_requests_both_remain_held(self):
        reserve_once(self.db, self.request_id, "a" * 64, "b" * 64)
        for request in (self.request_id, str(uuid.uuid4())):
            with self.assertRaises(DispatchHeld):
                reserve_once(self.db, request, "a" * 64, "b" * 64)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM studio_dispatch_hold").fetchone()[0], 1)

    def test_invalid_input_performs_no_write(self):
        for request, target in (("not-a-uuid", "a" * 64), (self.request_id, "private-data")):
            with self.assertRaises(ValueError):
                reserve_once(self.db, request, target, "b" * 64)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0], 0)

    def test_active_transaction_is_refused(self):
        self.db.execute("BEGIN")
        with self.assertRaises(ValueError):
            reserve_once(self.db, self.request_id, "a" * 64, "b" * 64)
        self.assertTrue(self.db.in_transaction)


if __name__ == "__main__":
    unittest.main()
