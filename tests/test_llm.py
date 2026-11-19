import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from app.llm import Action, LLMClient, LLMError, Plan, validate_plan


class LLMTests(unittest.TestCase):
    def test_rejects_unsafe_actions(self):
        for action in [
            Action(device_id="climate", values={"temperature_c": 35}),
            Action(device_id="dnd", values={"enabled": "true"}),
            Action(device_id="sound", values={"track_id": "unknown"}),
            Action(device_id="alarm", values={"at": "2026-11-13T07:00+03:00"}),
        ]:
            with self.subTest(action=action), self.assertRaises(LLMError):
                validate_plan(Plan(outcome="done", reason="", actions=[action]), ["track_01"])

    @patch.dict("os.environ", {"LLM_PROVIDER": "google", "LLM_API_KEY": "test-key", "LLM_MODEL": "test-model", "LLM_BASE_URL": "https://example.test/v1"}, clear=True)
    def test_other_providers_are_rejected(self):
        with self.assertRaises(LLMError):
            LLMClient()

    @patch.dict("os.environ", {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model", "LLM_BASE_URL": "https://example.test/v1"}, clear=True)
    def test_invalid_plan_is_retried_before_execution(self):
        invalid = Plan(outcome="done", reason="", actions=[Action(device_id="light", values={"enabled": True})])
        valid = Plan(outcome="done", reason="", actions=[Action(device_id="light", values={"brightness": 0})])
        client = LLMClient()
        with patch.object(client, "_generate", side_effect=[invalid, valid]) as generate:
            result = client.plan("Выключи свет", {}, "2026-11-12T20:00:00+03:00", [], {}, [], [])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(result.actions[0].values, {"brightness": 0})

    @patch.dict("os.environ", {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model", "LLM_BASE_URL": "https://example.test/v1"}, clear=True)
    def test_persistently_invalid_plan_returns_refusal(self):
        invalid = Plan(outcome="done", reason="", actions=[Action(device_id="light", values={"enabled": True})])
        client = LLMClient()
        with patch.object(client, "_generate", return_value=invalid) as generate:
            result = client.plan("Выключи свет", {}, "2026-11-12T20:00:00+03:00", [], {}, [], [])
        self.assertEqual(generate.call_count, 3)
        self.assertEqual(result.outcome, "refused")
        self.assertEqual(result.actions, [])

    @patch.dict("os.environ", {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model", "LLM_BASE_URL": "https://example.test/v1"}, clear=True)
    def test_service_error_is_not_treated_as_bad_model_output(self):
        client = LLMClient()
        with patch.object(client, "_generate", side_effect=LLMError("LLM HTTP 401")) as generate:
            with self.assertRaises(LLMError):
                client.plan("тест", {}, "2026-11-12T20:00:00+03:00", [], {}, [], [])
        self.assertEqual(generate.call_count, 1)

    @patch.dict("os.environ", {"LLM_PROVIDER": "sain", "LLM_API_KEY": "test-key", "LLM_MODEL": "test-model", "LLM_BASE_URL": "https://example.test/v1"}, clear=True)
    @patch("app.llm.urlopen")
    def test_proxy_uses_chat_completion(self, urlopen):
        answer = {"outcome": "refused", "reason": "medical", "actions": []}
        response = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
        urlopen.return_value.__enter__.return_value = io.BytesIO(json.dumps(response).encode())
        result = LLMClient().plan("что выпить", {}, "2026-11-12T20:00:00+03:00", [], {}, [], [])
        self.assertEqual(result.reason, "medical")
        self.assertEqual(urlopen.call_args.args[0].full_url, "https://example.test/v1/chat/completions")

    @patch.dict("os.environ", {"LLM_API_KEY": "test-key", "LLM_MODEL": "test-model", "LLM_BASE_URL": "https://example.test/v1"}, clear=True)
    @patch("app.llm.urlopen")
    def test_http_errors_do_not_expose_credentials(self, urlopen):
        urlopen.side_effect = HTTPError("https://example.test", 401, "unauthorized", None, None)
        with self.assertRaises(LLMError) as error:
            LLMClient().plan("test", {}, "2026-11-12T20:00:00+03:00", [], {}, [], [])
        self.assertNotIn("test-key", str(error.exception))
        self.assertIn("401", str(error.exception))


if __name__ == "__main__":
    unittest.main()
