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

"""In-memory doubles for Gerrit, GitHub and the git mirror."""

from __future__ import annotations

from typing import Any, Iterator, Optional

from gerrit_github_archiver.github import UnprocessableEntity


class FakeGerrit:
    def __init__(self, comments: dict[str, list[dict]] | None = None) -> None:
        self.comments = comments or {}

    def get_comments(self, change_id: str) -> dict[str, list[dict]]:
        return self.comments


class FakeMirror:
    def __init__(self) -> None:
        self.pushes: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.ensured: list[str] = []

    def push_archive_head(self, url: str, sha: str, branch: str) -> None:
        self.pushes.append((sha, branch))

    def delete_archive_head(self, url: str, branch: str) -> None:
        self.deleted.append(branch)

    def commit_parent(self, sha: str) -> Optional[str]:
        return "p" * 40

    def has_commit(self, sha: str) -> bool:
        return True

    def ensure_commit(self, sha: str) -> bool:
        self.ensured.append(sha)
        return True

    def fetch(self) -> None:
        pass


class FakeGitHub:
    """Records every write so tests can assert on duplicates.

    `reject_inline` makes the batched review endpoint raise 422 the way GitHub
    does when a comment falls outside a diff hunk.
    """

    def __init__(self, reject_inline: bool = False, reject_individual: bool = False) -> None:
        self.prs: dict[int, dict] = {}
        self.reviews: list[dict] = []
        self.review_comments: list[dict] = []
        self.issue_comments: list[dict] = []
        self.updates: list[tuple[int, dict]] = []
        self.ready_calls: list[str] = []
        self._next_id = 1
        self._reject_inline = reject_inline
        self._reject_individual = reject_individual

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def find_pr_by_head(self, head_branch: str) -> Optional[dict]:
        for pr in self.prs.values():
            if pr["head"]["ref"] == head_branch:
                return pr
        return None

    def create_pr(self, *, title, head, base, body, draft=False) -> dict:
        number = self._id()
        pr = {
            "number": number,
            "node_id": f"node-{number}",
            "title": title,
            "body": body,
            "state": "open",
            "draft": draft,
            "head": {"ref": head, "sha": "a" * 40},
            "base": {"ref": base},
        }
        self.prs[number] = pr
        return pr

    def get_pr(self, number: int) -> dict:
        return self.prs[number]

    def update_pr(self, number: int, **fields: Any) -> dict:
        self.updates.append((number, fields))
        self.prs[number].update(fields)
        return self.prs[number]

    def create_review(self, number, *, body, comments, event="COMMENT") -> dict:
        if comments and self._reject_inline:
            raise UnprocessableEntity("line must be part of the diff")
        review = {"id": self._id(), "body": body, "comments": list(comments)}
        self.reviews.append(review)
        self.review_comments.extend(comments)
        return review

    def create_review_comment(self, number, **fields: Any) -> dict:
        if self._reject_individual:
            raise UnprocessableEntity("line must be part of the diff")
        created = {"id": self._id(), **fields}
        self.review_comments.append(created)
        return created

    def create_issue_comment(self, number: int, body: str) -> dict:
        created = {"id": self._id(), "body": body}
        self.issue_comments.append(created)
        return created

    def list_issue_comments(self, number: int) -> Iterator[dict]:
        yield from list(self.issue_comments)

    def list_review_comments(self, number: int) -> Iterator[dict]:
        yield from list(self.review_comments)

    def list_reviews(self, number: int) -> Iterator[dict]:
        yield from list(self.reviews)

    def mark_ready_for_review(self, node_id: str) -> None:
        self.ready_calls.append(node_id)
        for pr in self.prs.values():
            if pr["node_id"] == node_id:
                pr["draft"] = False
