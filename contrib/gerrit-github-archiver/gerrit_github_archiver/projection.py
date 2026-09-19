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

"""Pure mapping from Gerrit review data onto GitHub payloads.

Nothing here performs I/O, so the fiddly parts (comment anchoring, review
grouping, attribution) are unit-testable in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .ledger import KIND_COMMENT, KIND_MESSAGE, marker

# Gerrit pseudo-files that have no position in a GitHub diff.
COMMIT_MSG = "/COMMIT_MSG"
MERGE_LIST = "/MERGE_LIST"
PATCHSET_LEVEL = "/PATCHSET_LEVEL"
PSEUDO_FILES = frozenset({COMMIT_MSG, MERGE_LIST, PATCHSET_LEVEL})


def parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse a Gerrit timestamp (``YYYY-MM-DD HH:MM:SS.ffffffffff``, UTC)."""
    if not value:
        return None
    text = value.strip()
    # Gerrit emits nanosecond precision; datetime handles at most microseconds.
    if "." in text:
        head, _, frac = text.partition(".")
        text = f"{head}.{frac[:6]}"
        fmt = "%Y-%m-%d %H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%d %H:%M:%S"
    try:
        return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def format_timestamp(value: Optional[str]) -> str:
    parsed = parse_timestamp(value)
    return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else (value or "unknown time")


def format_author(account: Optional[dict]) -> str:
    """Render a Gerrit account as durable text.

    Deliberately textual: GitHub actorship degrades to `ghost` when an account
    is deleted, whereas a name written into the body survives indefinitely.
    """
    if not account:
        return "Unknown"
    name = account.get("name") or account.get("username") or "Unknown"
    email = account.get("email")
    return f"{name} <{email}>" if email else str(name)


def attribution(account: Optional[dict], when: Optional[str], patch_set: Optional[int]) -> str:
    parts = [f"**{format_author(account)}**", format_timestamp(when)]
    if patch_set is not None:
        parts.append(f"Patch Set {patch_set}")
    return " · ".join(parts)


def github_side(comment: dict) -> str:
    """Map a Gerrit comment side onto a GitHub diff side."""
    return "LEFT" if comment.get("side") == "PARENT" else "RIGHT"


def anchor_for(comment: dict) -> Optional[dict]:
    """Return GitHub line-anchor fields, or None when the comment cannot be inline.

    GitHub rejects review comments that fall outside a diff hunk with 422, and
    has no position for Gerrit's pseudo-files or for file-level comments, so
    those are routed to the fallback path by the caller.
    """
    path = comment.get("path")
    if not path or path in PSEUDO_FILES:
        return None

    rng = comment.get("range")
    line = comment.get("line")
    if rng:
        end_line = rng.get("end_line")
        start_line = rng.get("start_line")
        if not end_line:
            return None
        anchor: dict[str, Any] = {"line": end_line, "side": github_side(comment)}
        # GitHub rejects start_line == line, so only emit a real multi-line span.
        if start_line and start_line < end_line:
            anchor["start_line"] = start_line
            anchor["start_side"] = github_side(comment)
        return anchor
    if line:
        return {"line": line, "side": github_side(comment)}
    # No line and no range means a file-level comment in Gerrit.
    return None


def render_comment_body(comment: dict) -> str:
    """Render one inline comment, with attribution and provenance marker."""
    head = attribution(
        comment.get("author"), comment.get("updated"), comment.get("patch_set")
    )
    lines = [head, marker(KIND_COMMENT, comment["id"]), "", comment.get("message", "")]
    if comment.get("unresolved"):
        lines += ["", "_Marked unresolved in Gerrit._"]
    return "\n".join(lines).strip()


def render_fallback_body(comment: dict) -> str:
    """Render a comment that could not be anchored to a diff line.

    `context_lines` comes from Gerrit's comment API and holds the actual source
    the reviewer was looking at, so the quote survives even though the anchor
    does not.
    """
    path = comment.get("path", "(unknown file)")
    location = path
    if path == COMMIT_MSG:
        location = "commit message"
    elif path == PATCHSET_LEVEL:
        location = "patch set (no file)"
    line = comment.get("line") or (comment.get("range") or {}).get("end_line")
    if line and path not in PSEUDO_FILES:
        location = f"`{path}` line {line}"
    elif path not in PSEUDO_FILES:
        location = f"`{path}` (file-level)"

    parts = [
        attribution(
            comment.get("author"), comment.get("updated"), comment.get("patch_set")
        ),
        marker(KIND_COMMENT, comment["id"]),
        "",
        f"On {location}:",
    ]
    context = comment.get("context_lines") or []
    if context:
        quoted = "\n".join(
            f"{c.get('line_number', '')}\t{c.get('context_line', '')}".rstrip()
            for c in context
        )
        parts += ["", "```", quoted, "```"]
    parts += ["", comment.get("message", "")]
    if comment.get("unresolved"):
        parts += ["", "_Marked unresolved in Gerrit._"]
    return "\n".join(parts).strip()


def render_message_body(message: dict) -> str:
    """Render a Gerrit change message (a review action, including votes)."""
    head = attribution(
        message.get("author"), message.get("date"), message.get("_revision_number")
    )
    return "\n".join(
        [head, marker(KIND_MESSAGE, message["id"]), "", message.get("message", "")]
    ).strip()


def render_pr_body(change: dict, gerrit_url: str) -> str:
    """Body of the archive pull request itself."""
    number = change.get("_number")
    owner = format_author(change.get("owner"))
    lines = [
        "_Archived Gerrit review. This pull request is a read-only record; "
        "the review happened in Gerrit._",
        "",
        f"* **Gerrit change:** [{number}]({gerrit_url.rstrip('/')}/c/"
        f"{change.get('project')}/+/{number})",
        f"* **Change-Id:** `{change.get('change_id')}`",
        f"* **Owner:** {owner}",
        f"* **Target branch:** `{change.get('branch')}`",
    ]
    if change.get("topic"):
        lines.append(f"* **Topic:** `{change['topic']}`")
    lines.append(f"* **Created:** {format_timestamp(change.get('created'))}")
    return "\n".join(lines)


def is_autogenerated(message: dict) -> bool:
    return str(message.get("tag") or "").startswith("autogenerated:")


def group_comments_by_message(comments_by_path: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Group inline comments by the review action that published them.

    Gerrit links each comment to its change message via `change_message_id`,
    which is what lets one Gerrit review become one GitHub review. Older data
    may lack the link, so those fall back to a synthetic key derived from the
    author and timestamp.
    """
    grouped: dict[str, list[dict]] = {}
    for path, comments in sorted(comments_by_path.items()):
        for comment in comments:
            enriched = dict(comment)
            enriched.setdefault("path", path)
            key = enriched.get("change_message_id")
            if not key:
                author = (enriched.get("author") or {}).get("_account_id", "?")
                key = f"synthetic:{enriched.get('patch_set')}:{author}:{enriched.get('updated')}"
            grouped.setdefault(key, []).append(enriched)
    for comments in grouped.values():
        comments.sort(key=lambda c: (c.get("path", ""), c.get("line") or 0))
    return grouped


@dataclass
class ReviewPlan:
    """One Gerrit review action, ready to be posted to GitHub."""

    message_id: str
    body: str
    inline: list[dict] = field(default_factory=list)
    fallback: list[dict] = field(default_factory=list)
    comment_ids: list[str] = field(default_factory=list)
    # Every Gerrit comment in this review, kept so a payload GitHub rejects
    # can be mapped back to its source for the issue-comment fallback.
    source_comments: list[dict] = field(default_factory=list)
    # True when the review body already reached GitHub on an earlier pass and
    # only some of its comments are outstanding. Without this, a crash partway
    # through the per-comment fallback would strand the remaining comments:
    # the message would look synced and its comments would never be retried.
    body_already_posted: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.body.strip() and not self.inline and not self.fallback


def _fill_plan(plan: ReviewPlan, comments: list[dict]) -> ReviewPlan:
    for comment in comments:
        plan.comment_ids.append(comment["id"])
        plan.source_comments.append(comment)
        anchor = anchor_for(comment)
        if anchor is None:
            plan.fallback.append(comment)
        else:
            plan.inline.append(
                {"path": comment["path"], "body": render_comment_body(comment), **anchor}
            )
    return plan


def plan_reviews(
    change: dict,
    comments_by_path: dict[str, list[dict]],
    already_synced_messages: set[str],
    already_synced_comments: Optional[set[str]] = None,
    *,
    skip_autogenerated: bool = True,
) -> list[ReviewPlan]:
    """Compute the reviews that still need projecting, oldest first.

    Everything already recorded in the ledger is filtered out here, which is
    what makes a re-run a no-op. Message-level and comment-level state are
    tracked separately so that a review whose body posted but whose comments
    did not is resumed rather than skipped.
    """
    synced_comments = already_synced_comments or set()
    grouped = group_comments_by_message(comments_by_path)
    plans: list[ReviewPlan] = []

    def outstanding(comments: list[dict]) -> list[dict]:
        return [c for c in comments if c["id"] not in synced_comments]

    for message in change.get("messages") or []:
        message_id = message.get("id")
        if not message_id:
            continue
        comments = outstanding(grouped.pop(message_id, []))
        body_posted = message_id in already_synced_messages
        if body_posted and not comments:
            continue
        if skip_autogenerated and is_autogenerated(message) and not comments:
            continue

        plan = _fill_plan(
            ReviewPlan(
                message_id=message_id,
                body=render_message_body(message),
                body_already_posted=body_posted,
            ),
            comments,
        )
        if not plan.is_empty:
            plans.append(plan)

    # Comments whose change message is missing (deleted message, or data from
    # before change_message_id existed) still need archiving.
    for key, comments in sorted(grouped.items()):
        comments = outstanding(comments)
        body_posted = key in already_synced_messages
        if not comments:
            continue
        plans.append(
            _fill_plan(
                ReviewPlan(
                    message_id=key,
                    body="_Review comments from Gerrit._",
                    body_already_posted=body_posted,
                ),
                comments,
            )
        )

    return plans
