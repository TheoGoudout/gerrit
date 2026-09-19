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

"""Tests for the webhook receiver."""

import base64
import json
import threading
import unittest
import urllib.error
import urllib.request

from gerrit_github_archiver.webhook import (
    ChangeQueue,
    WebhookReceiver,
    WebhookServer,
    extract_change,
)


def event(project="myproject", number=42, etype="comment-added"):
    """A Gerrit stream event, shaped as the webhooks plugin serialises it."""
    return {
        "type": etype,
        "project": project,
        "refName": "refs/heads/main",
        "change": {
            "project": project,
            "branch": "main",
            "number": number,
            "id": "I1234",
            "subject": "Fix the thing",
            "status": "NEW",
        },
    }


class ExtractChangeTest(unittest.TestCase):
    def test_extracts_project_and_number(self):
        self.assertEqual(extract_change(event()), ("myproject", 42))

    def test_falls_back_to_top_level_project(self):
        payload = event()
        del payload["change"]["project"]
        self.assertEqual(extract_change(payload), ("myproject", 42))

    def test_ignores_events_without_a_change(self):
        # ref-updated and project-created carry no change; not our concern.
        self.assertIsNone(extract_change({"type": "ref-updated"}))
        self.assertIsNone(extract_change({"type": "x", "change": "not-a-dict"}))

    def test_ignores_malformed_number(self):
        payload = event()
        payload["change"]["number"] = "not-a-number"
        self.assertIsNone(extract_change(payload))


class ChangeQueueTest(unittest.TestCase):
    def test_coalesces_repeat_keys(self):
        q = ChangeQueue()
        self.assertTrue(q.put("a", 1))
        self.assertFalse(q.put("a", 2))
        self.assertEqual(q.depth, 1)
        key, value = q.get()
        self.assertEqual((key, value), ("a", 2))

    def test_preserves_arrival_order(self):
        q = ChangeQueue()
        for key in "abc":
            q.put(key, key)
        self.assertEqual([q.get()[0] for _ in range(3)], ["a", "b", "c"])

    def test_in_flight_key_is_not_handed_out_twice(self):
        q = ChangeQueue()
        q.put("a", 1)
        q.get()
        self.assertFalse(q.put("a", 2))
        self.assertIsNone(q.get(timeout=0.01))

    def test_events_during_flight_requeue_once(self):
        """A comment posted mid-projection must not be lost."""
        q = ChangeQueue()
        q.put("a", 1)
        q.get()
        q.put("a", 2)
        q.put("a", 3)
        q.task_done("a")
        self.assertEqual(q.get(timeout=0.1), ("a", 3))

    def test_quiet_completion_does_not_requeue(self):
        q = ChangeQueue()
        q.put("a", 1)
        q.get()
        q.task_done("a")
        self.assertIsNone(q.get(timeout=0.01))

    def test_overflow_sheds_load(self):
        q = ChangeQueue(maxsize=2)
        self.assertTrue(q.put("a", 1))
        self.assertTrue(q.put("b", 1))
        self.assertFalse(q.put("c", 1))
        self.assertEqual(q.dropped, 1)
        self.assertEqual(q.depth, 2)

    def test_get_times_out_when_empty(self):
        self.assertIsNone(ChangeQueue().get(timeout=0.01))

    def test_close_releases_waiters(self):
        q = ChangeQueue()
        released = threading.Event()

        def wait():
            q.get(timeout=5)
            released.set()

        t = threading.Thread(target=wait, daemon=True)
        t.start()
        q.close()
        self.assertTrue(released.wait(2), "close() did not release the waiter")


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.receiver = WebhookReceiver("s3cret", lambda p, n: None)

    def headers(self, mapping):
        return mapping

    def test_basic_auth_password_accepted(self):
        encoded = base64.b64encode(b"gerrit:s3cret").decode()
        self.assertTrue(
            self.receiver.check_auth({"Authorization": f"Basic {encoded}"}, "")
        )

    def test_wrong_basic_password_rejected(self):
        encoded = base64.b64encode(b"gerrit:wrong").decode()
        self.assertFalse(
            self.receiver.check_auth({"Authorization": f"Basic {encoded}"}, "")
        )

    def test_malformed_basic_header_rejected(self):
        self.assertFalse(self.receiver.check_auth({"Authorization": "Basic !!!"}, ""))

    def test_header_token_accepted(self):
        self.assertTrue(self.receiver.check_auth({"X-Archiver-Token": "s3cret"}, ""))

    def test_query_token_accepted(self):
        self.assertTrue(self.receiver.check_auth({}, "token=s3cret"))

    def test_missing_credential_rejected(self):
        self.assertFalse(self.receiver.check_auth({}, ""))

    def test_empty_token_disables_auth(self):
        open_receiver = WebhookReceiver("", lambda p, n: None)
        self.assertTrue(open_receiver.check_auth({}, ""))


class ServerTest(unittest.TestCase):
    """Drives the receiver over real HTTP."""

    def setUp(self):
        self.seen: list[tuple[str, int]] = []
        self.gate = threading.Event()
        self.gate.set()

        def handler(project, number):
            self.gate.wait(5)
            self.seen.append((project, number))

        self.server = WebhookServer(
            host="127.0.0.1",
            port=0,
            path="/gerrit-event",
            token="s3cret",
            handler=handler,
        )
        self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def post(self, payload, path="/gerrit-event", token="s3cret"):
        req = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Archiver-Token": token},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def wait_for_seen(self, count, timeout=5.0):
        deadline = threading.Event()
        for _ in range(int(timeout * 100)):
            if len(self.seen) >= count:
                return True
            deadline.wait(0.01)
        return False

    def test_accepted_delivery_reaches_the_worker(self):
        status, _ = self.post(event())
        self.assertEqual(status, 202)
        self.assertTrue(self.wait_for_seen(1))
        self.assertEqual(self.seen, [("myproject", 42)])

    def test_bad_token_is_rejected(self):
        status, _ = self.post(event(), token="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(self.seen, [])

    def test_wrong_path_is_404(self):
        self.assertEqual(self.post(event(), path="/nope")[0], 404)

    def test_health_endpoint(self):
        with urllib.request.urlopen(f"{self.base}/healthz", timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_invalid_json_is_rejected(self):
        req = urllib.request.Request(
            f"{self.base}/gerrit-event",
            data=b"{not json",
            headers={"X-Archiver-Token": "s3cret"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_event_without_a_change_is_accepted_but_ignored(self):
        status, body = self.post({"type": "ref-updated"})
        self.assertEqual(status, 202)
        self.assertIn("ignored", body)

    def test_handler_never_blocks_the_response(self):
        """The plugin drops events after ~5s, so responses must not wait."""
        self.gate.clear()
        try:
            for _ in range(3):
                status, _ = self.post(event())
                self.assertEqual(status, 202)
            self.assertEqual(self.seen, [])
        finally:
            self.gate.set()
        self.assertTrue(self.wait_for_seen(1))

    def test_burst_for_one_change_coalesces(self):
        self.gate.clear()
        for _ in range(5):
            self.post(event())
        self.gate.set()
        self.assertTrue(self.wait_for_seen(1))
        # One in flight plus at most one re-queued run; never five.
        self.assertLessEqual(len(self.seen), 2)


if __name__ == "__main__":
    unittest.main()
