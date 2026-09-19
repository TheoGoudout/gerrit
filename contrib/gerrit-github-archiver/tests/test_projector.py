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

"""End-to-end projector behaviour against in-memory doubles.

The central property under test is convergence: projecting the same change
twice must leave exactly one pull request and one copy of every comment.
"""

import unittest

from gerrit_github_archiver.config import Config, GerritConfig, GitHubConfig, ProjectMapping
from gerrit_github_archiver.ledger import Ledger
from gerrit_github_archiver.projector import Projector

from .fakes import FakeGerrit, FakeGitHub, FakeMirror

SHA = "b" * 40


def make_change(**kw):
    change = {
        "_number": 42,
        "project": "myproject",
        "branch": "main",
        "change_id": "I1234",
        "subject": "Fix the thing",
        "status": "NEW",
        "created": "2026-03-14 09:00:00.000000000",
        "updated": "2026-03-14 09:30:00.000000000",
        "owner": {"_account_id": 1000, "name": "Alice Smith", "email": "a@x.com"},
        "revisions": {SHA: {"_number": 1}},
        "current_revision": SHA,
        "messages": [
            {
                "id": "m1",
                "date": "2026-03-14 09:22:33.000000000",
                "message": "Patch Set 1: Code-Review+2",
                "_revision_number": 1,
                "author": {"_account_id": 1001, "name": "Bob Jones", "email": "b@x.com"},
            }
        ],
    }
    change.update(kw)
    return change


def make_comment(cid="c1", **kw):
    base = {
        "id": cid,
        "patch_set": 1,
        "line": 12,
        "updated": "2026-03-14 09:22:33.000000000",
        "message": "needs a null check",
        "change_message_id": "m1",
        "author": {"_account_id": 1001, "name": "Bob Jones", "email": "b@x.com"},
    }
    base.update(kw)
    return base


class ProjectorTestBase(unittest.TestCase):
    def build(self, *, comments=None, github=None, **config_kw):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.close)
        mapping = ProjectMapping(
            gerrit_project="myproject",
            github=GitHubConfig(owner="o", repo="r", token="t", git_url="url"),
        )
        config = Config(
            gerrit=GerritConfig(url="https://gerrit.example", username="bot", token="t"),
            projects=(mapping,),
            **config_kw,
        )
        self.gerrit = FakeGerrit(comments or {})
        self.github = github or FakeGitHub()
        self.mirror = FakeMirror()
        return Projector(
            config, mapping, self.gerrit, self.github, self.mirror, self.ledger
        )


class ConvergenceTest(ProjectorTestBase):
    def test_first_run_creates_pr_and_review(self):
        projector = self.build(comments={"src/a.java": [make_comment()]})
        result = projector.project(make_change())

        self.assertTrue(result.created_pr)
        self.assertEqual(result.reviews_posted, 1)
        self.assertEqual(result.comments_posted, 1)
        self.assertEqual(len(self.github.prs), 1)
        self.assertEqual(len(self.github.reviews), 1)
        self.assertEqual(self.mirror.pushes, [(SHA, "gerrit-archive/42")])

    def test_second_run_is_a_no_op(self):
        projector = self.build(comments={"src/a.java": [make_comment()]})
        change = make_change()
        projector.project(change)
        result = projector.project(change)

        self.assertFalse(result.created_pr)
        self.assertEqual(result.reviews_posted, 0)
        self.assertEqual(len(self.github.prs), 1)
        self.assertEqual(len(self.github.reviews), 1)
        self.assertEqual(len(self.github.review_comments), 1)

    def test_new_review_on_second_run_is_appended(self):
        comments = {"src/a.java": [make_comment()]}
        projector = self.build(comments=comments)
        projector.project(make_change())

        change = make_change()
        change["messages"].append(
            {
                "id": "m2",
                "date": "2026-03-15 10:00:00.000000000",
                "message": "Patch Set 1: Code-Review-1",
                "_revision_number": 1,
                "author": {"_account_id": 1002, "name": "Carol", "email": "c@x.com"},
            }
        )
        comments["src/b.java"] = [
            make_comment("c2", change_message_id="m2", line=5)
        ]
        result = projector.project(change)

        self.assertEqual(result.reviews_posted, 1)
        self.assertEqual(len(self.github.reviews), 2)
        self.assertEqual(len(self.github.review_comments), 2)

    def test_ledger_loss_recovers_via_markers(self):
        """A wiped ledger must not duplicate: adoption re-reads the markers."""
        comments = {"src/a.java": [make_comment()]}
        projector = self.build(comments=comments)
        projector.project(make_change())
        reviews_before = len(self.github.reviews)

        # Same GitHub state, brand new ledger, as after losing the volume.
        fresh = self.build(comments=comments, github=self.github)
        result = fresh.project(make_change())

        self.assertEqual(result.reviews_posted, 0)
        self.assertEqual(len(self.github.reviews), reviews_before)
        self.assertEqual(len(self.github.prs), 1)


class CrashResumeTest(ProjectorTestBase):
    def test_review_body_without_comments_is_resumed(self):
        """Simulates dying between posting a review body and its comments."""
        from gerrit_github_archiver.ledger import KIND_MESSAGE, change_key

        projector = self.build(comments={"src/a.java": [make_comment()]})
        key = change_key("myproject", 42)
        # Pre-seed the ledger as if the body had posted and the process died.
        self.ledger.upsert_change(key, "myproject", 42)
        self.ledger.record_synced(key, KIND_MESSAGE, "m1", "999")

        result = projector.project(make_change())

        self.assertEqual(result.comments_posted, 1)
        # No second review body for the same Gerrit message.
        self.assertEqual(len(self.github.reviews), 0)
        self.assertEqual(len(self.github.review_comments), 1)

    def test_resumed_review_is_not_repeated_again(self):
        from gerrit_github_archiver.ledger import KIND_MESSAGE, change_key

        projector = self.build(comments={"src/a.java": [make_comment()]})
        key = change_key("myproject", 42)
        self.ledger.upsert_change(key, "myproject", 42)
        self.ledger.record_synced(key, KIND_MESSAGE, "m1", "999")
        projector.project(make_change())
        result = projector.project(make_change())

        self.assertEqual(result.comments_posted, 0)
        self.assertEqual(len(self.github.review_comments), 1)


class FallbackTest(ProjectorTestBase):
    def test_rejected_batch_retries_per_comment(self):
        projector = self.build(
            comments={"src/a.java": [make_comment(), make_comment("c2", line=20)]},
            github=FakeGitHub(reject_inline=True),
        )
        result = projector.project(make_change())

        self.assertEqual(result.reviews_posted, 1)
        self.assertEqual(result.comments_posted, 2)
        self.assertEqual(len(self.github.review_comments), 2)

    def test_unanchorable_comment_becomes_issue_comment(self):
        projector = self.build(
            comments={"src/a.java": [make_comment()]},
            github=FakeGitHub(reject_inline=True, reject_individual=True),
        )
        result = projector.project(make_change())

        self.assertEqual(result.fallbacks_posted, 1)
        self.assertEqual(len(self.github.issue_comments), 1)
        self.assertIn("needs a null check", self.github.issue_comments[0]["body"])

    def test_commit_message_comment_skips_inline_entirely(self):
        projector = self.build(
            comments={"/COMMIT_MSG": [make_comment(path="/COMMIT_MSG", line=7)]}
        )
        result = projector.project(make_change())

        self.assertEqual(result.comments_posted, 0)
        self.assertEqual(result.fallbacks_posted, 1)
        self.assertIn("commit message", self.github.issue_comments[0]["body"])


class VisibilityTest(ProjectorTestBase):
    def test_private_change_is_never_projected(self):
        projector = self.build()
        result = projector.project(make_change(is_private=True))

        self.assertEqual(result.skipped_reason, "private")
        self.assertEqual(self.github.prs, {})
        self.assertEqual(self.mirror.pushes, [])

    def test_branch_filter_excludes_other_branches(self):
        self.ledger = Ledger(":memory:")
        self.addCleanup(self.ledger.close)
        mapping = ProjectMapping(
            gerrit_project="myproject",
            github=GitHubConfig(owner="o", repo="r", token="t", git_url="url"),
            branches=("main",),
        )
        config = Config(
            gerrit=GerritConfig(url="https://g", username="bot", token="t"),
            projects=(mapping,),
        )
        github = FakeGitHub()
        projector = Projector(
            config, mapping, FakeGerrit(), github, FakeMirror(), self.ledger
        )
        result = projector.project(make_change(branch="experimental"))

        self.assertIn("not archived", result.skipped_reason or "")
        self.assertEqual(github.prs, {})

    def test_change_from_another_project_is_refused(self):
        # Defence in depth: the sweep query scopes by project, but a mismatch
        # must never push one project's change into another's repository.
        projector = self.build()
        result = projector.project(make_change(project="other-project"))

        self.assertIn("not mapped here", result.skipped_reason or "")
        self.assertEqual(self.github.prs, {})
        self.assertEqual(self.mirror.pushes, [])

    def test_dry_run_writes_nothing(self):
        projector = self.build(comments={"a.java": [make_comment()]}, dry_run=True)
        result = projector.project(make_change())

        self.assertEqual(result.skipped_reason, "dry-run")
        self.assertEqual(self.github.prs, {})
        self.assertEqual(self.mirror.pushes, [])


class LifecycleTest(ProjectorTestBase):
    def test_wip_change_opens_a_draft_pr(self):
        projector = self.build()
        projector.project(make_change(work_in_progress=True))
        self.assertTrue(next(iter(self.github.prs.values()))["draft"])

    def test_leaving_wip_marks_pr_ready(self):
        projector = self.build()
        projector.project(make_change(work_in_progress=True))
        projector.project(make_change(updated="2026-03-14 10:00:00.000000000"))
        self.assertEqual(len(self.github.ready_calls), 1)
        self.assertFalse(next(iter(self.github.prs.values()))["draft"])

    def test_abandoned_change_closes_pr(self):
        projector = self.build()
        projector.project(make_change())
        projector.project(
            make_change(status="ABANDONED", updated="2026-03-14 11:00:00.000000000")
        )
        self.assertEqual(next(iter(self.github.prs.values()))["state"], "closed")

    def test_merged_change_is_left_for_github_to_close(self):
        # Replication advancing the base branch is what flips the badge to
        # Merged; an explicit close would show it as merely Closed.
        projector = self.build()
        projector.project(make_change())
        projector.project(
            make_change(status="MERGED", updated="2026-03-14 11:00:00.000000000")
        )
        self.assertEqual(next(iter(self.github.prs.values()))["state"], "open")

    def test_new_patch_set_force_pushes_head(self):
        projector = self.build()
        projector.project(make_change())
        ps2 = "c" * 40
        projector.project(
            make_change(
                revisions={SHA: {"_number": 1}, ps2: {"_number": 2}},
                current_revision=ps2,
                updated="2026-03-14 12:00:00.000000000",
            )
        )
        self.assertEqual(
            self.mirror.pushes,
            [(SHA, "gerrit-archive/42"), (ps2, "gerrit-archive/42")],
        )


if __name__ == "__main__":
    unittest.main()
