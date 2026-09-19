# Copyright (C) 2026 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest

from gerrit_github_archiver.ledger import (
    KIND_COMMENT,
    KIND_MESSAGE,
    Ledger,
    change_key,
    marker,
    parse_marker,
)


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.ledger = Ledger(":memory:")
        self.key = change_key("myproject", 42)

    def tearDown(self):
        self.ledger.close()

    def test_upsert_preserves_unspecified_fields(self):
        self.ledger.upsert_change(
            self.key, "myproject", 42, pr_number=7, head_branch="b", status="NEW"
        )
        self.ledger.upsert_change(self.key, "myproject", 42, last_updated="t2")
        record = self.ledger.get_change(self.key)
        self.assertEqual(record.pr_number, 7)
        self.assertEqual(record.head_branch, "b")
        self.assertEqual(record.last_updated, "t2")

    def test_record_synced_is_idempotent(self):
        self.assertTrue(self.ledger.record_synced(self.key, KIND_COMMENT, "u1", "99"))
        self.assertFalse(self.ledger.record_synced(self.key, KIND_COMMENT, "u1", "99"))
        self.assertTrue(self.ledger.is_synced(self.key, KIND_COMMENT, "u1"))

    def test_kinds_are_independent(self):
        self.ledger.record_synced(self.key, KIND_COMMENT, "x")
        self.assertFalse(self.ledger.is_synced(self.key, KIND_MESSAGE, "x"))

    def test_changes_are_independent(self):
        other = change_key("myproject", 43)
        self.ledger.record_synced(self.key, KIND_COMMENT, "x")
        self.assertFalse(self.ledger.is_synced(other, KIND_COMMENT, "x"))

    def test_synced_ids_returns_set(self):
        self.ledger.record_synced(self.key, KIND_MESSAGE, "m1")
        self.ledger.record_synced(self.key, KIND_MESSAGE, "m2")
        self.assertEqual(self.ledger.synced_ids(self.key, KIND_MESSAGE), {"m1", "m2"})

    def test_patchset_round_trip(self):
        self.assertIsNone(self.ledger.pushed_sha(self.key, 1))
        self.ledger.record_patchset(self.key, 1, "a" * 40)
        self.assertEqual(self.ledger.pushed_sha(self.key, 1), "a" * 40)

    def test_survives_reopen(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "l.sqlite3")
            first = Ledger(path)
            first.record_synced(self.key, KIND_COMMENT, "u1")
            first.close()
            second = Ledger(path)
            try:
                self.assertTrue(second.is_synced(self.key, KIND_COMMENT, "u1"))
            finally:
                second.close()


class MarkerTest(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(
            parse_marker(f"text {marker(KIND_MESSAGE, 'abc-123')} more"),
            (KIND_MESSAGE, "abc-123"),
        )

    def test_absent_marker(self):
        self.assertIsNone(parse_marker("plain body"))
        self.assertIsNone(parse_marker(None))
        self.assertIsNone(parse_marker(""))


if __name__ == "__main__":
    unittest.main()
