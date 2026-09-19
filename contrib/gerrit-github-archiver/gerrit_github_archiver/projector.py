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

"""Project one Gerrit change onto one GitHub pull request.

Every GitHub write is gated on the ledger, so calling :meth:`Projector.project`
repeatedly for the same change converges instead of duplicating.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .config import Config, ProjectMapping
from .gerrit import GerritClient
from .github import GitHubClient, UnprocessableEntity
from .gitops import Mirror
from .ledger import KIND_COMMENT, KIND_MESSAGE, Ledger, change_key, parse_marker
from .projection import (
    ReviewPlan,
    plan_reviews,
    render_fallback_body,
    render_pr_body,
)

logger = logging.getLogger(__name__)


@dataclass
class ProjectionResult:
    change_key: str
    pr_number: Optional[int] = None
    created_pr: bool = False
    reviews_posted: int = 0
    comments_posted: int = 0
    fallbacks_posted: int = 0
    skipped_reason: Optional[str] = None
    # Set when a human has to look at something the archiver cannot fix.
    needs_attention: bool = False


class Projector:
    def __init__(
        self,
        config: Config,
        mapping: ProjectMapping,
        gerrit: GerritClient,
        github: GitHubClient,
        mirror: Mirror,
        ledger: Ledger,
    ) -> None:
        self._config = config
        self._mapping = mapping
        self._gerrit = gerrit
        self._github = github
        self._mirror = mirror
        self._ledger = ledger

    # -- helpers ---------------------------------------------------------

    def _branch_for(self, change_number: int) -> str:
        return f"{self._mapping.branch_prefix}/{change_number}"

    def _title_for(self, change: dict) -> str:
        return f"{change.get('subject', '(no subject)')} (Gerrit {change.get('_number')})"

    def _current_revision_sha(self, change: dict) -> Optional[str]:
        """Return the SHA of the highest-numbered patch set."""
        revisions = change.get("revisions") or {}
        if not revisions:
            return change.get("current_revision")
        best_sha, best_num = None, -1
        for sha, info in revisions.items():
            num = info.get("_number", 0)
            if num > best_num:
                best_sha, best_num = sha, num
        return best_sha

    # -- adoption --------------------------------------------------------

    def _adopt_existing(self, key: str, pr_number: int) -> int:
        """Re-import already-posted GitHub items into the ledger.

        This closes the crash window between a successful GitHub write and the
        corresponding ledger write: anything carrying our provenance marker is
        recognised as already projected, so it is never posted twice.
        """
        adopted = 0
        for item in self._github.list_issue_comments(pr_number):
            parsed = parse_marker(item.get("body"))
            if parsed and self._ledger.record_synced(key, parsed[0], parsed[1], item.get("id")):
                adopted += 1
        for item in self._github.list_review_comments(pr_number):
            parsed = parse_marker(item.get("body"))
            if parsed and self._ledger.record_synced(key, parsed[0], parsed[1], item.get("id")):
                adopted += 1
        for review in self._github.list_reviews(pr_number):
            parsed = parse_marker(review.get("body"))
            if parsed and self._ledger.record_synced(key, parsed[0], parsed[1], review.get("id")):
                adopted += 1
        if adopted:
            logger.info("adopted %d pre-existing GitHub items for %s", adopted, key)
        return adopted

    # -- pull request lifecycle ------------------------------------------

    def _ensure_head_pushed(self, key: str, change: dict, branch: str) -> Optional[str]:
        sha = self._current_revision_sha(change)
        if not sha:
            return None
        current_number = (change.get("revisions") or {}).get(sha, {}).get("_number")
        if current_number is not None and self._ledger.pushed_sha(key, current_number) == sha:
            return sha
        self._mirror.ensure_commit(sha)
        self._mirror.push_archive_head(self._mapping.github.git_url, sha, branch)
        if current_number is not None:
            self._ledger.record_patchset(key, current_number, sha)
        return sha

    def _create_pr(self, change: dict, branch: str, sha: str, draft: bool) -> dict:
        body = render_pr_body(change, self._gerrit_web_url())
        base = change.get("branch") or "main"
        try:
            return self._github.create_pr(
                title=self._title_for(change),
                head=branch,
                base=base,
                body=body,
                draft=draft,
            )
        except UnprocessableEntity as exc:
            if "No commits between" not in str(exc):
                raise
            # The change already landed and was replicated, so head is an
            # ancestor of base and GitHub sees an empty diff. Pin a synthetic
            # base at the commit's parent so the diff still renders. The PR
            # then closes rather than showing a Merged badge, which the body
            # records explicitly.
            parent = self._mirror.commit_parent(sha)
            if not parent:
                raise
            base_branch = f"{branch}-base"
            self._mirror.push_archive_head(
                self._mapping.github.git_url, parent, base_branch
            )
            logger.info(
                "change already merged upstream; pinning synthetic base %s", base_branch
            )
            return self._github.create_pr(
                title=self._title_for(change),
                head=branch,
                base=base_branch,
                body=body
                + "\n\n_Archived after the change had already merged, so the base "
                "is pinned to the parent commit and GitHub shows this as closed "
                "rather than merged._",
                draft=draft,
            )

    def _ensure_pr(self, key: str, change: dict, branch: str, sha: str) -> tuple[dict, bool]:
        record = self._ledger.get_change(key)
        if record and record.pr_number:
            return self._github.get_pr(record.pr_number), False

        existing = self._github.find_pr_by_head(branch)
        if existing:
            logger.info("adopting existing PR #%s for %s", existing["number"], key)
            return existing, False

        draft = bool(change.get("work_in_progress"))
        pr = self._create_pr(change, branch, sha, draft)
        logger.info("created PR #%s for %s", pr["number"], key)
        return pr, True

    def _sync_pr_state(self, change: dict, pr: dict) -> None:
        """Align title, body and open/closed/draft state with Gerrit."""
        desired_title = self._title_for(change)
        fields: dict[str, str] = {}
        if pr.get("title") != desired_title:
            fields["title"] = desired_title

        status = change.get("status")
        # A merged change is closed by GitHub itself once replication advances
        # the base branch, so only ABANDONED needs an explicit close.
        if status == "ABANDONED" and pr.get("state") == "open":
            fields["state"] = "closed"
        elif status == "NEW" and pr.get("state") == "closed":
            fields["state"] = "open"

        if fields:
            self._github.update_pr(pr["number"], **fields)

        if pr.get("draft") and not change.get("work_in_progress"):
            node_id = pr.get("node_id")
            if node_id:
                self._github.mark_ready_for_review(node_id)

    # -- review projection -----------------------------------------------

    def _post_fallbacks(self, key: str, pr_number: int, comments: list[dict]) -> int:
        posted = 0
        for comment in comments:
            if self._ledger.is_synced(key, KIND_COMMENT, comment["id"]):
                continue
            created = self._github.create_issue_comment(
                pr_number, render_fallback_body(comment)
            )
            self._ledger.record_synced(
                key, KIND_COMMENT, comment["id"], created.get("id")
            )
            posted += 1
        return posted

    def _post_comments_individually(
        self, key: str, pr_number: int, inline: list[dict], head_sha: str
    ) -> tuple[int, list[dict]]:
        """Post inline comments one at a time, collecting the unanchorable ones.

        Used both when GitHub rejects a batched review and when resuming a
        review whose body already posted on an earlier pass.
        """
        posted = 0
        stranded: list[dict] = []
        for comment in inline:
            parsed = parse_marker(comment["body"])
            gerrit_id = parsed[1] if parsed else None
            if gerrit_id and self._ledger.is_synced(key, KIND_COMMENT, gerrit_id):
                continue
            try:
                created = self._github.create_review_comment(
                    pr_number, commit_id=head_sha, **comment
                )
                if gerrit_id:
                    self._ledger.record_synced(
                        key, KIND_COMMENT, gerrit_id, created.get("id")
                    )
                posted += 1
            except UnprocessableEntity:
                stranded.append(comment)
        return posted, stranded

    def _resolve_stranded(
        self, stranded: list[dict], plan: ReviewPlan
    ) -> list[dict]:
        """Map rejected GitHub payloads back to their Gerrit comments."""
        resolved: list[dict] = []
        for comment in stranded:
            parsed = parse_marker(comment["body"])
            gerrit_id = parsed[1] if parsed else None
            original = next(
                (c for c in plan.source_comments if c.get("id") == gerrit_id), None
            )
            resolved.append(
                original
                or {
                    "id": gerrit_id,
                    "path": comment.get("path"),
                    "line": comment.get("line"),
                    "message": comment.get("body"),
                }
            )
        return resolved

    def _post_review(
        self, key: str, pr_number: int, plan: ReviewPlan, head_sha: str
    ) -> tuple[int, int]:
        """Post one Gerrit review as one GitHub review.

        On 422 the batch is retried per comment, because GitHub rejects the
        whole review when any single comment falls outside a diff hunk and does
        not say which one. Comments that still fail become issue comments.
        """
        inline_posted = 0
        fallbacks = list(plan.fallback)

        if plan.body_already_posted:
            # The body reached GitHub on an earlier pass; only the comments
            # are outstanding, so do not create a second review.
            inline_posted, stranded = self._post_comments_individually(
                key, pr_number, plan.inline, head_sha
            )
            fallbacks.extend(self._resolve_stranded(stranded, plan))
        else:
            try:
                review = self._github.create_review(
                    pr_number, body=plan.body, comments=plan.inline
                )
                self._ledger.record_synced(
                    key, KIND_MESSAGE, plan.message_id, review.get("id")
                )
                for comment in plan.inline:
                    parsed = parse_marker(comment["body"])
                    if parsed:
                        self._ledger.record_synced(key, parsed[0], parsed[1])
                inline_posted = len(plan.inline)
            except UnprocessableEntity as exc:
                logger.warning(
                    "batched review rejected for %s (%s); retrying comment by comment",
                    key,
                    str(exc)[:200],
                )
                review = self._github.create_review(pr_number, body=plan.body, comments=[])
                self._ledger.record_synced(
                    key, KIND_MESSAGE, plan.message_id, review.get("id")
                )
                inline_posted, stranded = self._post_comments_individually(
                    key, pr_number, plan.inline, head_sha
                )
                fallbacks.extend(self._resolve_stranded(stranded, plan))

        fallback_posted = self._post_fallbacks(key, pr_number, fallbacks)
        return inline_posted, fallback_posted

    def _gerrit_web_url(self) -> str:
        return self._config.gerrit.url

    # -- entry point -----------------------------------------------------

    def project(self, change: dict) -> ProjectionResult:
        project_name = change.get("project", self._mapping.gerrit_project)
        number = change["_number"]
        key = change_key(project_name, number)
        result = ProjectionResult(change_key=key)

        if self._config.skip_private and (
            change.get("is_private") or change.get("private")
        ):
            result.skipped_reason = "private"
            record = self._ledger.get_change(key)
            if record and record.pr_number:
                # Already published before it was made private. Nothing the
                # API can do undoes that, so make it loud rather than silent.
                logger.error(
                    "change %s became private but is already archived as %s#%s; "
                    "review that pull request manually",
                    key,
                    self._mapping.github.slug,
                    record.pr_number,
                )
                result.needs_attention = True
            return result

        if project_name != self._mapping.gerrit_project:
            # The sweep query already scopes by project, so reaching here means
            # something upstream is wrong. Refuse rather than push one
            # project's change into another project's GitHub repository.
            logger.warning(
                "refusing to project %s under the %s mapping",
                key,
                self._mapping.gerrit_project,
            )
            result.skipped_reason = f"project {project_name} not mapped here"
            return result

        if self._mapping.branches and change.get("branch") not in self._mapping.branches:
            result.skipped_reason = f"branch {change.get('branch')} not archived"
            return result

        if self._config.dry_run:
            result.skipped_reason = "dry-run"
            logger.info("[dry-run] would project %s", key)
            return result

        branch = self._branch_for(number)
        self._ledger.upsert_change(
            key, project_name, number, head_branch=branch, status=change.get("status")
        )

        sha = self._ensure_head_pushed(key, change, branch)
        if not sha:
            result.skipped_reason = "no revision"
            return result

        pr, created = self._ensure_pr(key, change, branch, sha)
        result.pr_number = pr["number"]
        result.created_pr = created
        self._ledger.upsert_change(key, project_name, number, pr_number=pr["number"])

        if not created:
            self._adopt_existing(key, pr["number"])

        self._sync_pr_state(change, pr)

        comments_by_path = self._gerrit.get_comments(str(number))
        plans = plan_reviews(
            change,
            comments_by_path,
            self._ledger.synced_ids(key, KIND_MESSAGE),
            self._ledger.synced_ids(key, KIND_COMMENT),
            skip_autogenerated=self._config.skip_autogenerated_messages,
        )
        for plan in plans:
            inline, fallback = self._post_review(
                key, pr["number"], plan, pr.get("head", {}).get("sha") or sha
            )
            result.reviews_posted += 1
            result.comments_posted += inline
            result.fallbacks_posted += fallback

        self._ledger.upsert_change(
            key,
            project_name,
            number,
            last_updated=change.get("updated"),
            status=change.get("status"),
        )
        return result
