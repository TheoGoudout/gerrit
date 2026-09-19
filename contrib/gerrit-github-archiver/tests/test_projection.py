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

"""Tests for the pure Gerrit -> GitHub mapping."""

import unittest

from gerrit_github_archiver.ledger import KIND_COMMENT, KIND_MESSAGE, parse_marker
from gerrit_github_archiver.projection import (
    COMMIT_MSG,
    PATCHSET_LEVEL,
    anchor_for,
    format_timestamp,
    group_comments_by_message,
    parse_timestamp,
    plan_reviews,
    render_comment_body,
    render_fallback_body,
    render_message_body,
)


def comment(cid, **kw):
    base = {
        "id": cid,
        "path": "src/a.java",
        "patch_set": 1,
        "updated": "2026-03-14 09:22:33.000000000",
        "author": {"_account_id": 1000, "name": "Alice Smith", "email": "a@x.com"},
        "message": "needs a null check",
    }
    base.update(kw)
    return base


def message(mid, text="Patch Set 1: Code-Review+2", **kw):
    base = {
        "id": mid,
        "date": "2026-03-14 09:22:33.000000000",
        "message": text,
        "_revision_number": 1,
        "author": {"_account_id": 1000, "name": "Alice Smith", "email": "a@x.com"},
    }
    base.update(kw)
    return base


class TimestampTest(unittest.TestCase):
    def test_parses_nanosecond_precision(self):
        parsed = parse_timestamp("2026-03-14 09:22:33.123456789")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.microsecond, 123456)

    def test_parses_without_fraction(self):
        self.assertIsNotNone(parse_timestamp("2026-03-14 09:22:33"))

    def test_formats_readably(self):
        self.assertEqual(
            format_timestamp("2026-03-14 09:22:33.000000000"), "2026-03-14 09:22 UTC"
        )

    def test_tolerates_garbage(self):
        self.assertIsNone(parse_timestamp("not a date"))
        self.assertEqual(format_timestamp(None), "unknown time")


class AnchorTest(unittest.TestCase):
    def test_single_line_maps_to_right_side(self):
        self.assertEqual(anchor_for(comment("c", line=42)), {"line": 42, "side": "RIGHT"})

    def test_parent_side_maps_to_left(self):
        anchor = anchor_for(comment("c", line=42, side="PARENT"))
        self.assertEqual(anchor["side"], "LEFT")

    def test_multiline_range_emits_start_line(self):
        anchor = anchor_for(
            comment("c", range={"start_line": 10, "end_line": 14})
        )
        self.assertEqual(anchor["line"], 14)
        self.assertEqual(anchor["start_line"], 10)
        self.assertEqual(anchor["start_side"], "RIGHT")

    def test_single_line_range_omits_start_line(self):
        # GitHub rejects start_line equal to line.
        anchor = anchor_for(comment("c", range={"start_line": 7, "end_line": 7}))
        self.assertEqual(anchor, {"line": 7, "side": "RIGHT"})

    def test_pseudo_files_are_not_anchorable(self):
        self.assertIsNone(anchor_for(comment("c", path=COMMIT_MSG, line=3)))
        self.assertIsNone(anchor_for(comment("c", path=PATCHSET_LEVEL)))

    def test_file_level_comment_is_not_anchorable(self):
        self.assertIsNone(anchor_for(comment("c", line=None)))


class MarkerRoundTripTest(unittest.TestCase):
    """Adoption after a crash depends on every body carrying a marker."""

    def test_inline_body_carries_comment_marker(self):
        body = render_comment_body(comment("uuid-1", line=4))
        self.assertEqual(parse_marker(body), (KIND_COMMENT, "uuid-1"))
        self.assertIn("Alice Smith", body)

    def test_fallback_body_carries_comment_marker(self):
        body = render_fallback_body(comment("uuid-2", path=COMMIT_MSG))
        self.assertEqual(parse_marker(body), (KIND_COMMENT, "uuid-2"))
        self.assertIn("commit message", body)

    def test_fallback_quotes_context_lines(self):
        body = render_fallback_body(
            comment(
                "uuid-3",
                line=12,
                context_lines=[{"line_number": 12, "context_line": "int x = null;"}],
            )
        )
        self.assertIn("int x = null;", body)

    def test_message_body_carries_message_marker(self):
        body = render_message_body(message("msg-1"))
        self.assertEqual(parse_marker(body), (KIND_MESSAGE, "msg-1"))
        self.assertIn("Code-Review+2", body)


class GroupingTest(unittest.TestCase):
    def test_groups_by_change_message_id(self):
        grouped = group_comments_by_message(
            {
                "a.java": [comment("c1", change_message_id="m1")],
                "b.java": [
                    comment("c2", path="b.java", change_message_id="m1"),
                    comment("c3", path="b.java", change_message_id="m2"),
                ],
            }
        )
        self.assertEqual(len(grouped["m1"]), 2)
        self.assertEqual(len(grouped["m2"]), 1)

    def test_synthesises_key_when_link_missing(self):
        grouped = group_comments_by_message({"a.java": [comment("c1")]})
        key = next(iter(grouped))
        self.assertTrue(key.startswith("synthetic:"))

    def test_injects_path_from_map_key(self):
        grouped = group_comments_by_message(
            {"dir/x.java": [{"id": "c", "message": "m", "change_message_id": "m1"}]}
        )
        self.assertEqual(grouped["m1"][0]["path"], "dir/x.java")


class PlanTest(unittest.TestCase):
    def _change(self, messages):
        return {"_number": 1, "project": "p", "messages": messages}

    def test_pairs_comments_with_their_review(self):
        change = self._change([message("m1")])
        plans = plan_reviews(
            change, {"a.java": [comment("c1", line=3, change_message_id="m1")]}, set()
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].message_id, "m1")
        self.assertEqual(len(plans[0].inline), 1)
        self.assertEqual(plans[0].comment_ids, ["c1"])

    def test_fully_synced_review_is_filtered(self):
        """Re-running must be a no-op; this is the idempotency guarantee."""
        change = self._change([message("m1")])
        comments = {"a.java": [comment("c1", line=3, change_message_id="m1")]}
        self.assertEqual(plan_reviews(change, comments, {"m1"}, {"c1"}), [])

    def test_message_with_no_comments_is_filtered_once_synced(self):
        change = self._change([message("m1")])
        self.assertEqual(plan_reviews(change, {}, {"m1"}, set()), [])

    def test_partially_synced_review_resumes_its_comments(self):
        """A crash between posting the body and its comments must be resumable.

        Tracking only the message id would mark the review done and strand the
        comments that never made it.
        """
        change = self._change([message("m1")])
        comments = {
            "a.java": [
                comment("c1", line=3, change_message_id="m1"),
                comment("c2", line=9, change_message_id="m1"),
            ]
        }
        plans = plan_reviews(change, comments, {"m1"}, {"c1"})
        self.assertEqual(len(plans), 1)
        self.assertTrue(plans[0].body_already_posted)
        self.assertEqual(plans[0].comment_ids, ["c2"])

    def test_synced_comments_are_dropped_from_a_new_review(self):
        change = self._change([message("m1")])
        comments = {
            "a.java": [
                comment("c1", line=3, change_message_id="m1"),
                comment("c2", line=9, change_message_id="m1"),
            ]
        }
        plans = plan_reviews(change, comments, set(), {"c1"})
        self.assertEqual(plans[0].comment_ids, ["c2"])
        self.assertFalse(plans[0].body_already_posted)

    def test_autogenerated_message_without_comments_is_skipped(self):
        change = self._change(
            [message("m1", "Uploaded patch set 2.", tag="autogenerated:gerrit:newPatchSet")]
        )
        self.assertEqual(plan_reviews(change, {}, set()), [])

    def test_autogenerated_message_with_comments_is_kept(self):
        change = self._change(
            [message("m1", "Patch Set 2:", tag="autogenerated:gerrit:newPatchSet")]
        )
        plans = plan_reviews(
            change, {"a.java": [comment("c1", line=3, change_message_id="m1")]}, set()
        )
        self.assertEqual(len(plans), 1)

    def test_autogenerated_kept_when_flag_disabled(self):
        change = self._change([message("m1", "x", tag="autogenerated:gerrit:merged")])
        self.assertEqual(len(plan_reviews(change, {}, set(), skip_autogenerated=False)), 1)

    def test_unanchorable_comments_route_to_fallback(self):
        change = self._change([message("m1")])
        plans = plan_reviews(
            change,
            {COMMIT_MSG: [comment("c1", path=COMMIT_MSG, line=2, change_message_id="m1")]},
            set(),
        )
        self.assertEqual(plans[0].inline, [])
        self.assertEqual(len(plans[0].fallback), 1)

    def test_orphan_comments_are_still_archived(self):
        # A comment whose change message was deleted must not be silently lost.
        change = self._change([])
        plans = plan_reviews(
            change, {"a.java": [comment("c1", line=3, change_message_id="gone")]}, set()
        )
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].comment_ids, ["c1"])

    def test_orphan_comments_are_not_reposted(self):
        change = self._change([])
        comments = {"a.java": [comment("c1", line=3, change_message_id="gone")]}
        self.assertEqual(plan_reviews(change, comments, {"gone"}, {"c1"}), [])

    def test_preserves_gerrit_message_order(self):
        change = self._change([message("m1"), message("m2"), message("m3")])
        plans = plan_reviews(change, {}, set())
        self.assertEqual([p.message_id for p in plans], ["m1", "m2", "m3"])


if __name__ == "__main__":
    unittest.main()
