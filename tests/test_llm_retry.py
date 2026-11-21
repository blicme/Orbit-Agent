import json
import unittest
import urllib.error
from unittest.mock import patch

from evoagent.llm import JsonChatClient
from evoagent.reviewer import OpenAICompatibleReviewer


class JsonChatRetryTests(unittest.TestCase):
    def test_retries_transient_failure_then_returns_json(self):
        calls = []

        def fake_urlopen(_request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    "http://example.test/v1/chat/completions",
                    429, "rate limited", None, None,
                )
            return _FakeResponse(
                json.dumps({
                    "choices": [{"message": {"content": '{"action": "final"}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                })
            )

        client = JsonChatClient(
            "http://example.test/v1", "key", "model",
            retries=2, retry_delay_seconds=0,
        )
        with patch("evoagent.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.complete_json("lead", "system", "user")

        self.assertEqual({"action": "final"}, result)
        self.assertEqual(2, len(calls))

    def test_openai_compatible_reviewer_retries_transient_failure(self):
        calls = []

        def fake_urlopen(_request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    "http://example.test/v1/chat/completions",
                    503, "temporarily unavailable", None, None,
                )
            return _FakeResponse(
                json.dumps({
                    "choices": [{"message": {"content": '{"findings": []}'}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                })
            )

        reviewer = OpenAICompatibleReviewer(
            "http://example.test/v1", "key", "model",
            retries=2, retry_delay_seconds=0,
        )
        with patch("evoagent.reviewer.urllib.request.urlopen", side_effect=fake_urlopen):
            result = reviewer._request_json({"model": "model"})

        self.assertEqual({"findings": []}, result)
        self.assertEqual(2, len(calls))


class JsonChatRepairTests(unittest.TestCase):
    def test_unparseable_content_is_repaired_with_one_follow_up_call(self):
        contents = ["not json at all", '{"action": "final", "findings": []}']
        payloads = []

        def fake_urlopen(request, timeout):
            payloads.append(json.loads(request.data.decode("utf-8")))
            return _FakeResponse(json.dumps({
                "choices": [{"message": {"content": contents[len(payloads) - 1]}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }))

        client = JsonChatClient(
            "http://example.test/v1", "key", "model",
            retries=2, retry_delay_seconds=0,
        )
        with patch("evoagent.llm.urllib.request.urlopen", side_effect=fake_urlopen):
            result = client.complete_json("lead", "system", "user")

        self.assertEqual({"action": "final", "findings": []}, result)
        self.assertEqual(2, len(payloads))
        self.assertEqual("assistant", payloads[1]["messages"][-2]["role"])


class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def read(self):
        return self._body.encode("utf-8")


if __name__ == "__main__":
    unittest.main()
