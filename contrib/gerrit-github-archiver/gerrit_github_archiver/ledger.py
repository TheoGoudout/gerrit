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

"""Durable record of what has already been projected onto GitHub.

The ledger is what makes the reconciler safe to re-run: every write to GitHub
is gated on a lookup here, and recorded here on success. It must survive
restarts, otherwise a redeploy would duplicate the whole archive.

Crash safety: a process that dies between the GitHub write and the ledger
write would re-post on the next pass. To close that window every projected
body carries an HTML marker (see :func:`marker`), and the projector adopts
pre-existing GitHub items back into the ledger before deciding what to post.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    change_key    TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    change_number INTEGER NOT NULL,
    pr_number     INTEGER,
    head_branch   TEXT,
    last_updated  TEXT,
    last_synced   TEXT,
    status        TEXT
);

CREATE TABLE IF NOT EXISTS patchsets (
    change_key TEXT NOT NULL,
    number     INTEGER NOT NULL,
    sha        TEXT NOT NULL,
    PRIMARY KEY (change_key, number)
);

-- One row per artefact projected onto GitHub. The UNIQUE key is the
-- duplicate-suppression mechanism; inserts use INSERT OR IGNORE so a racing
-- second writer cannot create a second copy.
CREATE TABLE IF NOT EXISTS synced_items (
    change_key TEXT NOT NULL,
    kind       TEXT NOT NULL,
    gerrit_id  TEXT NOT NULL,
    github_id  TEXT,
    PRIMARY KEY (change_key, kind, gerrit_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Kinds of projected artefact.
KIND_REVIEW = "review"
KIND_MESSAGE = "message"
KIND_COMMENT = "comment"

_MARKER_RE = re.compile(r"<!--\s*gga:(?P<kind>[a-z_]+):(?P<gerrit_id>[^\s>]+)\s*-->")


def marker(kind: str, gerrit_id: str) -> str:
    """Return the hidden provenance marker embedded in every projected body."""
    return f"<!-- gga:{kind}:{gerrit_id} -->"


def parse_marker(body: Optional[str]) -> Optional[tuple[str, str]]:
    """Extract ``(kind, gerrit_id)`` from a GitHub body, if it carries one."""
    if not body:
        return None
    m = _MARKER_RE.search(body)
    if not m:
        return None
    return m.group("kind"), m.group("gerrit_id")


@dataclass(frozen=True)
class ChangeRecord:
    change_key: str
    project: str
    change_number: int
    pr_number: Optional[int]
    head_branch: Optional[str]
    last_updated: Optional[str]
    status: Optional[str]


def change_key(project: str, change_number: int) -> str:
    return f"{project}~{change_number}"


class Ledger:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # -- changes ---------------------------------------------------------

    def get_change(self, key: str) -> Optional[ChangeRecord]:
        row = self._conn.execute(
            "SELECT * FROM changes WHERE change_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return ChangeRecord(
            change_key=row["change_key"],
            project=row["project"],
            change_number=row["change_number"],
            pr_number=row["pr_number"],
            head_branch=row["head_branch"],
            last_updated=row["last_updated"],
            status=row["status"],
        )

    def upsert_change(
        self,
        key: str,
        project: str,
        change_number: int,
        *,
        pr_number: Optional[int] = None,
        head_branch: Optional[str] = None,
        last_updated: Optional[str] = None,
        status: Optional[str] = None,
    ) -> None:
        """Insert or update a change row, leaving unspecified fields intact."""
        self._conn.execute(
            """
            INSERT INTO changes (change_key, project, change_number, pr_number,
                                 head_branch, last_updated, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(change_key) DO UPDATE SET
                pr_number    = COALESCE(excluded.pr_number, changes.pr_number),
                head_branch  = COALESCE(excluded.head_branch, changes.head_branch),
                last_updated = COALESCE(excluded.last_updated, changes.last_updated),
                status       = COALESCE(excluded.status, changes.status)
            """,
            (key, project, change_number, pr_number, head_branch, last_updated, status),
        )

    def mark_synced(self, key: str, when: str) -> None:
        self._conn.execute(
            "UPDATE changes SET last_synced = ? WHERE change_key = ?", (when, key)
        )

    # -- patch sets ------------------------------------------------------

    def pushed_sha(self, key: str, number: int) -> Optional[str]:
        row = self._conn.execute(
            "SELECT sha FROM patchsets WHERE change_key = ? AND number = ?",
            (key, number),
        ).fetchone()
        return row["sha"] if row else None

    def record_patchset(self, key: str, number: int, sha: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO patchsets (change_key, number, sha) VALUES (?, ?, ?)",
            (key, number, sha),
        )

    # -- projected artefacts ---------------------------------------------

    def is_synced(self, key: str, kind: str, gerrit_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM synced_items WHERE change_key = ? AND kind = ? AND gerrit_id = ?",
            (key, kind, gerrit_id),
        ).fetchone()
        return row is not None

    def synced_ids(self, key: str, kind: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT gerrit_id FROM synced_items WHERE change_key = ? AND kind = ?",
            (key, kind),
        ).fetchall()
        return {r["gerrit_id"] for r in rows}

    def record_synced(
        self, key: str, kind: str, gerrit_id: str, github_id: Optional[str] = None
    ) -> bool:
        """Record an artefact as projected.

        Returns True when this call created the row, False when it already
        existed. Callers treat False as "someone else already posted this".
        """
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO synced_items (change_key, kind, gerrit_id, github_id)
            VALUES (?, ?, ?, ?)
            """,
            (key, kind, gerrit_id, str(github_id) if github_id is not None else None),
        )
        return cur.rowcount > 0

    # -- global state ----------------------------------------------------

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )
