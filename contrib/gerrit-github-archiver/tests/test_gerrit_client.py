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

"""Tests for the Gerrit REST client."""

import unittest

from gerrit_github_archiver.gerrit import GerritClient, GerritError, _strip_xssi


class StripXssiTest(unittest.TestCase):
    def test_strips_prefix(self):
        self.assertEqual(_strip_xssi(")]}'\n[{\"a\": 1}]"), [{"a": 1}])

    def test_tolerates_missing_prefix(self):
        self.assertEqual(_strip_xssi('{"a": 1}'), {"a": 1})


class AnonymousModeTest(unittest.TestCase):
    def test_authenticated_client_uses_the_a_prefix(self):
        client = GerritClient("https://g.example", "bot", "t")
        self.assertFalse(client._anonymous)  # noqa: SLF001
        self.assertIsNotNone(client._session.auth)  # noqa: SLF001

    def test_anonymous_client_sends_no_credentials(self):
        client = GerritClient("https://g.example", "", "", anonymous=True)
        self.assertTrue(client._anonymous)  # noqa: SLF001
        self.assertIsNone(client._session.auth)  # noqa: SLF001


class GetCommentsTest(unittest.TestCase):
    """context_lines is only returned when enable-context is requested."""

    def setUp(self):
        self.client = GerritClient("https://g.example", "bot", "t")
        self.calls: list[tuple[str, dict]] = []

    def _stub(self, fail_first_with=None):
        def _get(path, params=None):
            self.calls.append((path, dict(params or {})))
            if fail_first_with and len(self.calls) == 1:
                raise GerritError(fail_first_with)
            return {"a.java": []}

        self.client._get = _get  # noqa: SLF001 - substituting the transport

    def test_requests_context_with_a_hyphen(self):
        # enable_context (underscore) is rejected by the server with 400.
        self._stub()
        self.client.get_comments("1")
        self.assertEqual(self.calls[0][1], {"enable-context": "true"})

    def test_falls_back_when_server_rejects_the_option(self):
        self._stub(fail_first_with="GET /changes/1/comments failed: 400 bad option")
        self.assertEqual(self.client.get_comments("1"), {"a.java": []})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][1], {})

    def test_other_errors_propagate(self):
        self._stub(fail_first_with="GET /changes/1/comments failed: 403 forbidden")
        with self.assertRaises(GerritError):
            self.client.get_comments("1")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
