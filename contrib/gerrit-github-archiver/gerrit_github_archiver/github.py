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

"""GitHub REST/GraphQL client, scoped to what the archiver needs."""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Iterator, Optional

import requests

logger = logging.getLogger(__name__)

# Raised as a distinct type because the projector treats it as "this comment
# cannot be anchored" rather than as a transport failure.
class UnprocessableEntity(Exception):
    def __init__(self, message: str, payload: Any = None) -> None:
        super().__init__(message)
        self.payload = payload


class GitHubError(Exception):
    pass


class GitHubClient:
    """Thin client with retry, primary and secondary rate-limit handling.

    GitHub enforces two separate limits: a primary hourly quota surfaced via
    x-ratelimit-remaining, and an undocumented secondary limit on
    content-creating requests that surfaces as 403/429 with retry-after. Both
    are honoured here; the archiver's volume is low, but a backfill sweep can
    trip the secondary limit easily.
    """

    def __init__(
        self,
        token: str,
        owner: str,
        repo: str,
        api_url: str = "https://api.github.com",
        timeout: int = 30,
        max_retries: int = 5,
    ) -> None:
        self._owner = owner
        self._repo = repo
        self._api = api_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "gerrit-github-archiver",
            }
        )

    @property
    def slug(self) -> str:
        return f"{self._owner}/{self._repo}"

    def _sleep_for_rate_limit(self, resp: requests.Response, attempt: int) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        if resp.headers.get("x-ratelimit-remaining") == "0":
            reset = resp.headers.get("x-ratelimit-reset")
            if reset:
                try:
                    return max(0.0, float(reset) - time.time()) + 1.0
                except ValueError:
                    pass
        # Exponential backoff with jitter for everything else.
        return min(60.0, (2**attempt)) + random.uniform(0, 1)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self._api}{path}"
        last_error: Optional[Exception] = None
        for attempt in range(self._max_retries):
            resp = self._session.request(
                method, url, json=json_body, params=params, timeout=self._timeout
            )
            if resp.status_code == 422:
                # Never retried: the payload is what GitHub objects to.
                raise UnprocessableEntity(
                    f"{method} {path}: {resp.text[:500]}", payload=json_body
                )
            if resp.status_code in (403, 429) or resp.status_code >= 500:
                delay = self._sleep_for_rate_limit(resp, attempt)
                logger.warning(
                    "%s %s -> %s, backing off %.1fs (attempt %d/%d)",
                    method,
                    path,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                last_error = GitHubError(f"{resp.status_code}: {resp.text[:300]}")
                time.sleep(delay)
                continue
            if not resp.ok:
                raise GitHubError(f"{method} {path}: {resp.status_code} {resp.text[:500]}")
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()
        raise GitHubError(f"{method} {path} exhausted retries") from last_error

    def paginate(self, path: str, params: Optional[dict] = None) -> Iterator[dict]:
        page_params = dict(params or {})
        page_params.setdefault("per_page", 100)
        url: Optional[str] = f"{self._api}{path}"
        while url:
            resp = self._session.get(
                url, params=page_params if url.startswith(self._api) else None,
                timeout=self._timeout,
            )
            if not resp.ok:
                raise GitHubError(f"GET {url}: {resp.status_code} {resp.text[:300]}")
            for item in resp.json():
                yield item
            url = resp.links.get("next", {}).get("url")

    # -- pull requests ---------------------------------------------------

    def find_pr_by_head(self, head_branch: str) -> Optional[dict]:
        """Locate a PR by head branch, open or closed.

        This is the recovery path for a crash between creating a PR and
        recording it in the ledger.
        """
        results = self.request(
            "GET",
            f"/repos/{self.slug}/pulls",
            params={
                "head": f"{self._owner}:{head_branch}",
                "state": "all",
                "per_page": 100,
            },
        )
        return results[0] if results else None

    def create_pr(
        self, *, title: str, head: str, base: str, body: str, draft: bool = False
    ) -> dict:
        return self.request(
            "POST",
            f"/repos/{self.slug}/pulls",
            json_body={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
                "draft": draft,
            },
        )

    def update_pr(self, number: int, **fields: Any) -> dict:
        return self.request(
            "PATCH", f"/repos/{self.slug}/pulls/{number}", json_body=fields
        )

    def get_pr(self, number: int) -> dict:
        return self.request("GET", f"/repos/{self.slug}/pulls/{number}")

    # -- comments and reviews --------------------------------------------

    def create_review(
        self, number: int, *, body: str, comments: list[dict], event: str = "COMMENT"
    ) -> dict:
        payload: dict[str, Any] = {"body": body, "event": event}
        if comments:
            payload["comments"] = comments
        return self.request(
            "POST", f"/repos/{self.slug}/pulls/{number}/reviews", json_body=payload
        )

    def create_review_comment(self, number: int, **fields: Any) -> dict:
        return self.request(
            "POST", f"/repos/{self.slug}/pulls/{number}/comments", json_body=fields
        )

    def create_issue_comment(self, number: int, body: str) -> dict:
        return self.request(
            "POST",
            f"/repos/{self.slug}/issues/{number}/comments",
            json_body={"body": body},
        )

    def list_issue_comments(self, number: int) -> Iterator[dict]:
        yield from self.paginate(f"/repos/{self.slug}/issues/{number}/comments")

    def list_review_comments(self, number: int) -> Iterator[dict]:
        yield from self.paginate(f"/repos/{self.slug}/pulls/{number}/comments")

    def list_reviews(self, number: int) -> Iterator[dict]:
        yield from self.paginate(f"/repos/{self.slug}/pulls/{number}/reviews")

    # -- GraphQL ---------------------------------------------------------

    def graphql(self, query: str, variables: dict[str, Any]) -> Any:
        resp = self.request(
            "POST",
            f"{self._api}/graphql",
            json_body={"query": query, "variables": variables},
        )
        if isinstance(resp, dict) and resp.get("errors"):
            raise GitHubError(f"graphql: {resp['errors']}")
        return resp

    def mark_ready_for_review(self, pr_node_id: str) -> None:
        """Flip a draft PR to ready. There is no REST equivalent."""
        self.graphql(
            """
            mutation($id: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $id}) {
                clientMutationId
              }
            }
            """,
            {"id": pr_node_id},
        )
