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

"""Receiver for Gerrit's webhooks plugin.

This is a latency optimisation, not a correctness mechanism. The plugin
retries a handful of times and then drops the event with nothing but a log
line, so no archive state may depend on an event having arrived. Everything
here does is ask the reconciler to look at one change sooner than its next
sweep would have.

Two consequences shape the design:

* The handler must answer fast. Anything slower than the plugin's retry
  budget turns a delivery into a permanent loss, so the handler validates,
  enqueues and returns 202 without touching Gerrit or GitHub.
* The event body is a trigger, not a payload. CommentAddedEvent carries the
  change message and approvals but no inline comments, so the worker re-reads
  the change from Gerrit rather than trusting the body.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import threading
from collections import OrderedDict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# Refuse bodies larger than this; Gerrit events are a few KB at most.
MAX_BODY_BYTES = 1 << 20


class ChangeQueue:
    """A coalescing work queue keyed by change.

    Two properties matter:

    * **Coalescing.** A burst of events for one change (patch set uploaded,
      then a review posted seconds later) collapses into a single unit of
      work, because the worker re-reads full state from Gerrit anyway.
    * **Serialisation.** A change already being projected is never handed to
      a second worker. If more events arrive while it is in flight, it is
      re-queued once on completion so the later state is not lost.
    """

    def __init__(self, maxsize: int = 1000) -> None:
        self._cond = threading.Condition()
        self._pending: "OrderedDict[str, Any]" = OrderedDict()
        self._in_flight: dict[str, Any] = {}
        self._redo: dict[str, Any] = {}
        self._maxsize = maxsize
        self._closed = False
        self.dropped = 0

    def put(self, key: str, value: Any) -> bool:
        """Enqueue a change. Returns False when coalesced, dropped or closed."""
        with self._cond:
            if self._closed:
                return False
            if key in self._in_flight:
                # Being worked on right now; remember to run it again after.
                self._redo[key] = value
                return False
            if key in self._pending:
                self._pending[key] = value
                return False
            if len(self._pending) >= self._maxsize:
                # The sweep is the backstop, so shedding load here is safe.
                self.dropped += 1
                logger.warning(
                    "queue full (%d); dropping %s, the next sweep will catch it",
                    self._maxsize,
                    key,
                )
                return False
            self._pending[key] = value
            self._cond.notify()
            return True

    def get(self, timeout: Optional[float] = None) -> Optional[tuple[str, Any]]:
        """Claim the oldest queued change, or None on timeout or close."""
        with self._cond:
            while not self._pending and not self._closed:
                if not self._cond.wait(timeout):
                    return None
            if not self._pending:
                return None
            key, value = self._pending.popitem(last=False)
            self._in_flight[key] = value
            return key, value

    def task_done(self, key: str) -> None:
        """Release a change, re-queueing it if events arrived meanwhile."""
        with self._cond:
            self._in_flight.pop(key, None)
            if key in self._redo:
                self._pending[key] = self._redo.pop(key)
                self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    @property
    def depth(self) -> int:
        with self._cond:
            return len(self._pending)


def extract_change(payload: dict) -> Optional[tuple[str, int]]:
    """Pull ``(project, change number)`` out of a Gerrit stream event.

    Returns None for events with no change (ref-updated, project-created),
    which are not this archiver's concern.
    """
    change = payload.get("change")
    if not isinstance(change, dict):
        return None
    number = change.get("number")
    project = change.get("project") or payload.get("project")
    if number is None or not project:
        return None
    try:
        return str(project), int(number)
    except (TypeError, ValueError):
        return None


class WebhookReceiver:
    """Glue between the HTTP handler and the projection workers."""

    def __init__(
        self,
        token: str,
        handler: Callable[[str, int], None],
        queue_size: int = 1000,
    ) -> None:
        self._token = token
        self._handler = handler
        self.queue = ChangeQueue(maxsize=queue_size)
        self.received = 0
        self.accepted = 0

    def check_auth(self, headers: Any, query: str) -> bool:
        """Authenticate a delivery.

        The webhooks plugin cannot sign payloads or set custom headers, so the
        only credential it can present is one embedded in the configured URL.
        Basic auth is the tidiest form of that; the header and query
        alternatives exist for proxies that strip credentials.
        """
        if not self._token:
            return True

        auth = headers.get("Authorization") or ""
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8", "replace")
            except (ValueError, UnicodeDecodeError):
                decoded = ""
            _, _, password = decoded.partition(":")
            if hmac.compare_digest(password, self._token):
                return True

        supplied = headers.get("X-Archiver-Token") or ""
        if supplied and hmac.compare_digest(supplied, self._token):
            return True

        from_query = (parse_qs(query).get("token") or [""])[0]
        return bool(from_query) and hmac.compare_digest(from_query, self._token)

    def enqueue(self, payload: dict) -> Optional[str]:
        """Queue the change named by an event. Returns its key, or None."""
        self.received += 1
        target = extract_change(payload)
        if target is None:
            return None
        project, number = target
        key = f"{project}~{number}"
        if self.queue.put(key, target):
            self.accepted += 1
        return key

    def work(self, stop: threading.Event) -> None:
        """Worker loop; runs until `stop` is set and the queue drains."""
        while not stop.is_set():
            item = self.queue.get(timeout=0.5)
            if item is None:
                continue
            key, (project, number) = item
            try:
                self._handler(project, number)
            except Exception:  # noqa: BLE001 - one bad change must not kill the worker
                logger.exception("webhook projection failed for %s", key)
            finally:
                self.queue.task_done(key)


def make_handler(receiver: WebhookReceiver, path: str) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "gerrit-github-archiver"
        sys_version = ""

        def _respond(self, status: HTTPStatus, body: str = "") -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            if encoded:
                self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
            if urlparse(self.path).path == "/healthz":
                self._respond(HTTPStatus.OK, "ok\n")
            else:
                self._respond(HTTPStatus.NOT_FOUND, "not found\n")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != path:
                self._respond(HTTPStatus.NOT_FOUND, "not found\n")
                return
            if not receiver.check_auth(self.headers, parsed.query):
                logger.warning("rejected unauthenticated delivery from %s", self.address_string())
                self._respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._respond(HTTPStatus.BAD_REQUEST, "bad content-length\n")
                return
            if length <= 0 or length > MAX_BODY_BYTES:
                self._respond(HTTPStatus.BAD_REQUEST, "bad body size\n")
                return

            try:
                payload = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self._respond(HTTPStatus.BAD_REQUEST, "invalid json\n")
                return
            if not isinstance(payload, dict):
                self._respond(HTTPStatus.BAD_REQUEST, "expected an object\n")
                return

            # Accepted, not processed: answering inside the plugin's retry
            # budget matters more than reporting the projection's outcome,
            # which the sweep would redo anyway.
            key = receiver.enqueue(payload)
            self._respond(HTTPStatus.ACCEPTED, f"{key or 'ignored'}\n")

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


class WebhookServer:
    """Threaded HTTP listener plus its pool of projection workers."""

    def __init__(
        self,
        host: str,
        port: int,
        path: str,
        token: str,
        handler: Callable[[str, int], None],
        workers: int = 1,
        queue_size: int = 1000,
    ) -> None:
        self.receiver = WebhookReceiver(token, handler, queue_size=queue_size)
        self._httpd = ThreadingHTTPServer(
            (host, port), make_handler(self.receiver, path)
        )
        self._httpd.daemon_threads = True
        self._stop = threading.Event()
        self._workers = [
            threading.Thread(
                target=self.receiver.work,
                args=(self._stop,),
                name=f"webhook-worker-{i}",
                daemon=True,
            )
            for i in range(max(1, workers))
        ]
        self._serve_thread: Optional[threading.Thread] = None
        if not token:
            logger.warning(
                "webhook receiver has no token; anyone who can reach %s:%d can "
                "queue work. Set webhook.token or WEBHOOK_TOKEN.",
                host,
                port,
            )

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> None:
        for worker in self._workers:
            worker.start()
        self._serve_thread = threading.Thread(
            target=self._httpd.serve_forever, name="webhook-http", daemon=True
        )
        self._serve_thread.start()
        logger.info("webhook receiver listening on %s", self._httpd.server_address)

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._stop.set()
        self.receiver.queue.close()
        for worker in self._workers:
            worker.join(timeout=5)
        if self._serve_thread:
            self._serve_thread.join(timeout=5)
