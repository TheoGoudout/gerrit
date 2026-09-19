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

"""Local mirror of a Gerrit project, and pushes of archive head refs.

The archiver keeps one bare mirror per Gerrit project so it can push patch set
commits to GitHub. The GitHub archive branch is what a pull request hangs off;
once the PR is closed the branch can be deleted, because GitHub retains
refs/pull/<n>/head indefinitely.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GitError(Exception):
    pass


def _redact(args: Sequence[str]) -> str:
    """Strip embedded credentials before a command reaches the log."""
    return " ".join(re.sub(r"://[^@/]+@", "://***@", a) for a in args)


class Mirror:
    def __init__(self, path: str, gerrit_git_url: str, dry_run: bool = False) -> None:
        self.path = os.path.abspath(path)
        self._gerrit_url = gerrit_git_url
        self._dry_run = dry_run

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = ["git", "--git-dir", self.path, *args]
        logger.debug("run: %s", _redact(cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False
        )
        if check and proc.returncode != 0:
            raise GitError(
                f"git {_redact(args)} failed ({proc.returncode}): {proc.stderr[:500]}"
            )
        return proc

    def ensure(self) -> None:
        """Create the bare mirror if it does not exist yet."""
        if os.path.isdir(os.path.join(self.path, "objects")):
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        logger.info("creating mirror at %s", self.path)
        proc = subprocess.run(
            ["git", "clone", "--bare", self._gerrit_url, self.path],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(f"clone failed: {proc.stderr[:500]}")

    def fetch(self) -> None:
        """Refresh branches and every patch set ref from Gerrit."""
        self.ensure()
        self._run(
            "fetch",
            "--prune",
            "--quiet",
            self._gerrit_url,
            "+refs/heads/*:refs/heads/*",
            "+refs/changes/*:refs/changes/*",
        )

    def ensure_commit(self, sha: str) -> bool:
        """Make `sha` available locally, fetching from Gerrit if needed.

        The webhook path reacts to a patch set within seconds of it being
        created, so the mirror is routinely one fetch behind. Fetching only on
        a miss keeps the common case free.
        """
        if self.has_commit(sha):
            return True
        logger.debug("commit %s missing from mirror; fetching", sha[:10])
        self.fetch()
        return self.has_commit(sha)

    def has_commit(self, sha: str) -> bool:
        if not _SHA_RE.match(sha or ""):
            return False
        proc = self._run("cat-file", "-e", f"{sha}^{{commit}}", check=False)
        return proc.returncode == 0

    def commit_parent(self, sha: str) -> Optional[str]:
        proc = self._run("rev-parse", f"{sha}^", check=False)
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def push_archive_head(self, github_git_url: str, sha: str, branch: str) -> None:
        """Force-push `sha` to `branch` on the GitHub remote.

        Force is intentional: each new Gerrit patch set replaces the archive
        head so that the final head SHA equals the commit that lands on the
        target branch. That identity is what makes GitHub associate the merge
        commit with this pull request, which in turn is what makes the review
        reachable from `git blame`.
        """
        if self._dry_run:
            logger.info("[dry-run] would push %s -> %s", sha[:10], branch)
            return
        if not self.has_commit(sha):
            raise GitError(f"commit {sha} not present in mirror; fetch first")
        self._run("push", "--force", github_git_url, f"{sha}:refs/heads/{branch}")
        logger.info("pushed %s -> %s", sha[:10], branch)

    def delete_archive_head(self, github_git_url: str, branch: str) -> None:
        if self._dry_run:
            logger.info("[dry-run] would delete branch %s", branch)
            return
        self._run("push", github_git_url, f":refs/heads/{branch}", check=False)
