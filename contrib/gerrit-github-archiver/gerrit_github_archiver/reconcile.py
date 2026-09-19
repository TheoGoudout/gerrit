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

"""The reconciler: the component that makes the archive correct.

Webhook delivery from Gerrit is best-effort — the webhooks plugin retries a
handful of times and then drops the event — so nothing may depend on an event
having arrived. This loop re-derives the desired state from Gerrit and
converges the GitHub side, and is therefore correct with webhooks disabled
entirely. The webhook path, when added, is only a latency optimisation that
calls the same projector.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from .config import Config, ProjectMapping
from .gerrit import GerritClient
from .github import GitHubClient
from .gitops import Mirror
from .ledger import Ledger, change_key
from .projector import Projector, ProjectionResult

logger = logging.getLogger(__name__)


@dataclass
class SweepStats:
    inspected: int = 0
    projected: int = 0
    skipped: int = 0
    unchanged: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, result: ProjectionResult) -> None:
        if result.skipped_reason:
            self.skipped += 1
        else:
            self.projected += 1


def build_query(mapping: ProjectMapping, since: Optional[datetime]) -> str:
    """Build the Gerrit query for one project.

    Private changes are excluded at the query level as well as in the
    projector, so a visibility mistake needs two independent failures.
    """
    parts = [f"project:{mapping.gerrit_project}", "-is:private"]
    if mapping.branches:
        branches = " OR ".join(f"branch:{b}" for b in mapping.branches)
        parts.append(f"({branches})")
    if since:
        parts.append(f'after:"{since.strftime("%Y-%m-%d %H:%M:%S")}"')
    return " ".join(parts)


class Reconciler:
    """Converges GitHub onto Gerrit's state.

    HTTP clients are built per thread rather than shared: the sweep and the
    webhook workers run concurrently, and `requests.Session` is not
    documented as thread-safe. The ledger is shared, and locks internally.
    """

    def __init__(self, config: Config, ledger: Ledger) -> None:
        self._config = config
        self._ledger = ledger
        self._local = threading.local()

    @property
    def _gerrit(self) -> GerritClient:
        return self._context()[0]

    def _context(self) -> tuple[GerritClient, dict[str, tuple[ProjectMapping, Projector]]]:
        """Return this thread's Gerrit client and per-project projectors."""
        existing = getattr(self._local, "context", None)
        if existing is not None:
            return existing
        gerrit = GerritClient(
            self._config.gerrit.url,
            self._config.gerrit.username,
            self._config.gerrit.token,
        )
        projectors = {}
        for mapping in self._config.projects:
            mirror = self._mirror_for(mapping)
            projectors[mapping.gerrit_project] = (
                mapping,
                self._projector_for(mapping, mirror, gerrit),
            )
        context = (gerrit, projectors)
        self._local.context = context
        return context

    def _mirror_for(self, mapping: ProjectMapping) -> Mirror:
        path = os.path.join(
            self._config.mirror_path, mapping.gerrit_project.replace("/", "_") + ".git"
        )
        return Mirror(
            path,
            f"{self._config.gerrit.git_url.rstrip('/')}/{mapping.gerrit_project}",
            dry_run=self._config.dry_run,
        )

    def _projector_for(
        self, mapping: ProjectMapping, mirror: Mirror, gerrit: GerritClient
    ) -> Projector:
        github = GitHubClient(
            mapping.github.token,
            mapping.github.owner,
            mapping.github.repo,
            api_url=mapping.github.api_url,
        )
        return Projector(self._config, mapping, gerrit, github, mirror, self._ledger)

    def project_change_number(
        self, project: str, number: int
    ) -> Optional[ProjectionResult]:
        """Project a single change, identified the way an event names it.

        This is the webhook path. It deliberately re-reads the change from
        Gerrit instead of trusting the event body, which carries no inline
        comments.
        """
        gerrit, projectors = self._context()
        entry = projectors.get(project)
        if entry is None:
            logger.debug("ignoring event for unconfigured project %s", project)
            return None
        _, projector = entry
        change = gerrit.get_change(f"{project}~{number}")
        result = projector.project(change)
        if result.reviews_posted or result.created_pr:
            logger.info(
                "[%s] webhook: PR #%s, %s reviews, %s inline, %s fallback%s",
                result.change_key,
                result.pr_number,
                result.reviews_posted,
                result.comments_posted,
                result.fallbacks_posted,
                " (created)" if result.created_pr else "",
            )
        return result

    def sweep_project(
        self, mapping: ProjectMapping, *, full: bool = False
    ) -> SweepStats:
        stats = SweepStats()
        since = None
        if not full:
            since = datetime.now(timezone.utc) - timedelta(
                minutes=self._config.lookback_minutes
            )
        query = build_query(mapping, since)
        logger.info("[%s] query: %s", mapping.gerrit_project, query)

        gerrit, projectors = self._context()
        mapping, projector = projectors[mapping.gerrit_project]
        if not self._config.dry_run:
            self._mirror_for(mapping).fetch()

        for change in gerrit.query_changes(
            query, page_size=self._config.page_size
        ):
            stats.inspected += 1
            key = change_key(change.get("project", mapping.gerrit_project), change["_number"])
            record = self._ledger.get_change(key)
            if (
                not full
                and record is not None
                and record.last_updated == change.get("updated")
            ):
                stats.unchanged += 1
                continue
            try:
                result = projector.project(change)
                stats.merge(result)
                if result.reviews_posted or result.created_pr:
                    logger.info(
                        "[%s] PR #%s: %s reviews, %s inline, %s fallback%s",
                        key,
                        result.pr_number,
                        result.reviews_posted,
                        result.comments_posted,
                        result.fallbacks_posted,
                        " (created)" if result.created_pr else "",
                    )
            except Exception as exc:  # noqa: BLE001 - one bad change must not stop the sweep
                stats.failed += 1
                stats.errors.append(f"{key}: {exc}")
                logger.exception("[%s] projection failed", key)
        return stats

    def sweep(self, *, full: bool = False) -> SweepStats:
        total = SweepStats()
        for mapping in self._config.projects:
            stats = self.sweep_project(mapping, full=full)
            total.inspected += stats.inspected
            total.projected += stats.projected
            total.skipped += stats.skipped
            total.unchanged += stats.unchanged
            total.failed += stats.failed
            total.errors.extend(stats.errors)
        self._ledger.set_meta("last_sweep", datetime.now(timezone.utc).isoformat())
        return total

    def run_forever(self) -> None:
        while True:
            started = time.monotonic()
            try:
                stats = self.sweep()
                logger.info(
                    "sweep done: %d inspected, %d projected, %d unchanged, "
                    "%d skipped, %d failed",
                    stats.inspected,
                    stats.projected,
                    stats.unchanged,
                    stats.skipped,
                    stats.failed,
                )
            except Exception:  # noqa: BLE001 - the loop must outlive any single sweep
                logger.exception("sweep failed")
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self._config.poll_interval_seconds - elapsed))
