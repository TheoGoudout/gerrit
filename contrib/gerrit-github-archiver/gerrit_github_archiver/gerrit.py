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

"""Read-only Gerrit REST client.

The archiver never writes to Gerrit. Everything here is a GET.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

# Gerrit prefixes every JSON response with this to defeat XSSI attacks.
_XSSI_PREFIX = ")]}'"

# Change detail options. ALL_REVISIONS is what lets us push every patch set;
# DETAILED_ACCOUNTS is what gives us real names instead of bare account ids.
CHANGE_OPTIONS = (
    "ALL_REVISIONS",
    "ALL_COMMITS",
    "DETAILED_ACCOUNTS",
    "DETAILED_LABELS",
    "MESSAGES",
    "CURRENT_COMMIT",
)


class GerritError(Exception):
    pass


def _strip_xssi(text: str) -> Any:
    if text.startswith(_XSSI_PREFIX):
        text = text[len(_XSSI_PREFIX) :]
    return json.loads(text.lstrip("\n"))


class GerritClient:
    def __init__(
        self,
        url: str,
        username: str,
        token: str,
        timeout: int = 30,
        anonymous: bool = False,
    ) -> None:
        self._base = url.rstrip("/")
        self._timeout = timeout
        self._anonymous = anonymous
        self._session = requests.Session()
        if not anonymous:
            # The /a/ path prefix selects Gerrit's authenticated endpoints.
            self._session.auth = (username, token)

    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        prefix = "" if self._anonymous else "/a"
        url = f"{self._base}{prefix}{path}"
        resp = self._session.get(url, params=params, timeout=self._timeout)
        if resp.status_code == 404:
            raise GerritError(f"not found: {path}")
        if not resp.ok:
            raise GerritError(
                f"GET {path} failed: {resp.status_code} {resp.text[:400]}"
            )
        return _strip_xssi(resp.text)

    def query_changes(
        self, query: str, *, page_size: int = 100, options: tuple[str, ...] = CHANGE_OPTIONS
    ) -> Iterator[dict[str, Any]]:
        """Yield ChangeInfo dicts for a query, following Gerrit's pagination.

        Gerrit signals more results with `_more_changes` on the last element
        rather than a total count, so we page until it stops appearing.
        """
        start = 0
        while True:
            params: list[tuple[str, Any]] = [
                ("q", query),
                ("n", page_size),
                ("S", start),
            ]
            params.extend(("o", opt) for opt in options)
            batch = self._get("/changes/", params=params)
            if not batch:
                return
            for change in batch:
                yield change
            if not batch[-1].get("_more_changes"):
                return
            start += len(batch)

    def get_change(self, change_id: str, options: tuple[str, ...] = CHANGE_OPTIONS) -> dict:
        params = [("o", opt) for opt in options]
        return self._get(f"/changes/{quote(change_id, safe='')}", params=params)

    def get_comments(self, change_id: str) -> dict[str, list[dict]]:
        """Return published inline comments as ``{path: [CommentInfo, ...]}``.

        `enable-context` is what makes Gerrit return `context_lines`, the
        source the reviewer was actually looking at. Comments that GitHub
        refuses to anchor fall back to quoting that, so without this
        parameter the fallback silently loses the code it refers to.

        The option is spelled with a hyphen (`--enable-context` in
        ListChangeComments); some versions' prose documents it with an
        underscore, which the server rejects outright. A server that refuses
        it is retried without, since losing context is better than losing the
        comments.

        Drafts are deliberately not fetched: they are unpublished by
        definition and archiving them would leak private notes.
        """
        path = f"/changes/{quote(change_id, safe='')}/comments"
        try:
            return self._get(path, params={"enable-context": "true"})
        except GerritError as exc:
            if "400" not in str(exc):
                raise
            logger.warning(
                "server rejected enable-context; falling back to comments "
                "without source context (%s)",
                str(exc)[:120],
            )
            return self._get(path)

    def get_patch(self, change_id: str, revision: str) -> str:
        """Return the raw diff of a revision (used only for diagnostics)."""
        return str(
            self._get(f"/changes/{quote(change_id, safe='')}/revisions/{revision}/patch")
        )
