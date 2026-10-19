import os
import json
import unittest
from unittest.mock import Mock, patch

import requests

from app.agent import (
    ModelAdapter,
    ModelConfigurationError,
    ModelResponseError,
    ModelUnavailableError,
)


class ModelAdapterTests(unittest.TestCase):
    def test_deterministic_response_has_clean_separator_and_label(self):
        with patch.dict(os.environ, {"MODEL_PROVIDER": "deterministic"}, clear=True):
            answer = ModelAdapter().answer("Hello", [], [])
        self.assertTrue(answer.startswith("[Development mode - deterministic response, not LLM output]"))
        self.assertNotIn("\ufffd", answer)

    def test_ollama_requires_url_and_model(self):
        cases = [
            ({"MODEL_PROVIDER": "ollama", "OLLAMA_MODEL": "local-model"}, "OLLAMA_URL"),
            ({"MODEL_PROVIDER": "ollama", "OLLAMA_URL": "http://localhost:11434/api/generate"}, "OLLAMA_MODEL"),
        ]
        for environment, missing_setting in cases:
            with self.subTest(missing_setting=missing_setting):
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(ModelConfigurationError, missing_setting):
                        ModelAdapter()

    def test_ollama_rejects_invalid_url_and_unknown_provider(self):
        for url in ("localhost:11434", "http://localhost:not-a-port/api/generate"):
            with self.subTest(url=url):
                with patch.dict(os.environ, {
                    "MODEL_PROVIDER": "ollama", "OLLAMA_URL": url, "OLLAMA_MODEL": "demo"
                }, clear=True):
                    with self.assertRaisesRegex(ModelConfigurationError, "valid absolute HTTP or HTTPS|absolute HTTP or HTTPS"):
                        ModelAdapter()
        with patch.dict(os.environ, {"MODEL_PROVIDER": "ollama-typo"}, clear=True):
            with self.assertRaisesRegex(ModelConfigurationError, "MODEL_PROVIDER"):
                ModelAdapter()

    def test_ollama_success_uses_configured_endpoint_and_model(self):
        response = Mock()
        response.json.return_value = {"response": "A real mocked model answer."}
        with patch.dict(os.environ, {
            "MODEL_PROVIDER": "ollama",
            "OLLAMA_URL": "http://localhost:11434/api/generate",
            "OLLAMA_MODEL": "test-model",
        }, clear=True), patch("app.agent.requests.post", return_value=response) as post:
            answer = ModelAdapter().answer("Question", [{"content": "Memory"}], [])

        self.assertEqual(answer, "A real mocked model answer.")
        self.assertEqual(post.call_args.args[0], "http://localhost:11434/api/generate")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "test-model")
        self.assertIn("Memory", post.call_args.kwargs["json"]["prompt"])

    def test_ollama_connection_timeout_and_http_errors_are_actionable(self):
        scenarios = [
            (requests.exceptions.ConnectionError(), "Could not connect to Ollama"),
            (requests.exceptions.Timeout(), "90-second timeout"),
        ]
        for failure, expected_message in scenarios:
            with self.subTest(failure=type(failure).__name__):
                with patch.dict(os.environ, {
                    "MODEL_PROVIDER": "ollama",
                    "OLLAMA_URL": "http://localhost:11434/api/generate",
                    "OLLAMA_MODEL": "test-model",
                }, clear=True), patch("app.agent.requests.post", side_effect=failure):
                    with self.assertRaisesRegex(ModelUnavailableError, expected_message):
                        ModelAdapter().answer("Question", [], [])

        response = Mock()
        response.status_code = 404
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
        with patch.dict(os.environ, {
            "MODEL_PROVIDER": "ollama",
            "OLLAMA_URL": "http://localhost:11434/api/generate",
            "OLLAMA_MODEL": "missing-model",
        }, clear=True), patch("app.agent.requests.post", return_value=response):
            with self.assertRaisesRegex(ModelUnavailableError, "HTTP 404.*ollama list"):
                ModelAdapter().answer("Question", [], [])

    def test_ollama_rejects_invalid_or_empty_response_payloads(self):
        response = Mock()
        response.raise_for_status.return_value = None
        scenarios = [
            (ValueError("invalid JSON"), "invalid JSON"),
            ({"response": "  "}, "no response text"),
            ({"unexpected": "shape"}, "no response text"),
        ]
        for payload, expected_message in scenarios:
            with self.subTest(payload=payload):
                if isinstance(payload, Exception):
                    response.json.side_effect = payload
                else:
                    response.json.side_effect = None
                    response.json.return_value = payload
                with patch.dict(os.environ, {
                    "MODEL_PROVIDER": "ollama",
                    "OLLAMA_URL": "http://localhost:11434/api/generate",
                    "OLLAMA_MODEL": "test-model",
                }, clear=True), patch("app.agent.requests.post", return_value=response):
                    with self.assertRaisesRegex(ModelResponseError, expected_message):
                        ModelAdapter().answer("Question", [], [])

    def test_groq_requires_key_and_requires_https_endpoint(self):
        with patch.dict(os.environ, {"MODEL_PROVIDER": "groq"}, clear=True):
            with self.assertRaisesRegex(ModelConfigurationError, "GROQ_API_KEY"):
                ModelAdapter()
        with patch.dict(os.environ, {
            "MODEL_PROVIDER": "groq", "GROQ_API_KEY": "test-only-key",
            "GROQ_BASE_URL": "http://api.groq.com/openai/v1/chat/completions",
        }, clear=True):
            with self.assertRaisesRegex(ModelConfigurationError, "HTTPS"):
                ModelAdapter()

    def test_groq_sends_only_given_context_and_returns_mocked_completion(self):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "Mocked Groq reply."}}]}
        memories = [{"content": "I prefer concise answers."}]
        recent = [{"role": "user", "content": "Earlier question"}]
        with patch.dict(os.environ, {
            "MODEL_PROVIDER": "groq", "GROQ_API_KEY": "test-only-key",
            "GROQ_MODEL": "openai/gpt-oss-20b",
        }, clear=True), patch("app.agent.requests.post", return_value=response) as post:
            adapter = ModelAdapter()
            answer = adapter.answer("Current request", memories, recent)

        self.assertEqual(answer, "Mocked Groq reply.")
        self.assertEqual(adapter.model_name, "openai/gpt-oss-20b")
        self.assertEqual(post.call_args.args[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-only-key")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "openai/gpt-oss-20b")
        self.assertIn("I prefer concise answers", post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertIn("Earlier question", post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertNotIn("test-only-key", str(post.call_args.kwargs["json"]))
        system=post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertIn("retrieved memories",system)
        self.assertIn("live web search",system)
        self.assertIn("clean Markdown",system)
        self.assertIn("recent turns",system)

    def test_groq_rate_limit_auth_and_invalid_response_errors_are_actionable(self):
        for status, expected in ((429, "rate limit"), (401, "GROQ_API_KEY"), (404, "GROQ_MODEL")):
            response = Mock()
            response.status_code = status
            response.raise_for_status.side_effect = requests.exceptions.HTTPError(response=response)
            with self.subTest(status=status), patch.dict(os.environ, {
                "MODEL_PROVIDER": "groq", "GROQ_API_KEY": "test-only-key"
            }, clear=True), patch("app.agent.requests.post", return_value=response):
                with self.assertRaisesRegex(ModelUnavailableError, expected):
                    ModelAdapter().answer("Question", [], [])

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": []}
        with patch.dict(os.environ, {
            "MODEL_PROVIDER": "groq", "GROQ_API_KEY": "test-only-key"
        }, clear=True), patch("app.agent.requests.post", return_value=response):
            with self.assertRaisesRegex(ModelResponseError, "invalid chat-completions"):
                ModelAdapter().answer("Question", [], [])

    def test_groq_structured_memory_analysis_is_mocked_and_message_only(self):
        analysis = {
            'message_type': 'assertion', 'about_user': True, 'confidence': 0.97,
            'candidate': {'content': 'The user prefers Python.', 'category': 'preference', 'polarity': 'positive'},
            'search_terms': ['preferred programming language', 'Python'],
        }
        response = Mock()
        response.json.return_value = {'choices': [{'message': {'content': json.dumps(analysis)}}]}
        with patch.dict(os.environ, {
            'MODEL_PROVIDER': 'groq', 'GROQ_API_KEY': 'test-only-key',
            'GROQ_MODEL': 'openai/gpt-oss-20b',
        }, clear=True), patch('app.agent.requests.post', return_value=response) as post:
            actual = ModelAdapter().analyze_memory_message('my fav prog lang is python')
        self.assertEqual(actual, analysis)
        sent = post.call_args.kwargs['json']['messages']
        self.assertEqual(len(sent), 2)
        self.assertIn('my fav prog lang is python', sent[1]['content'])
        self.assertNotIn('saved memories', sent[1]['content'])
        self.assertNotIn('test-only-key', str(post.call_args.kwargs['json']))

    def test_groq_memory_analysis_malformed_json_and_provider_failures_are_actionable(self):
        invalid = Mock()
        invalid.json.return_value = {'choices': [{'message': {'content': 'not json'}}]}
        with patch.dict(os.environ, {
            'MODEL_PROVIDER': 'groq', 'GROQ_API_KEY': 'test-only-key',
        }, clear=True), patch('app.agent.requests.post', return_value=invalid):
            with self.assertRaisesRegex(ModelResponseError, 'invalid JSON for memory interpretation'):
                ModelAdapter().analyze_memory_message('I prefer Python')
        with patch.dict(os.environ, {
            'MODEL_PROVIDER': 'groq', 'GROQ_API_KEY': 'test-only-key',
        }, clear=True), patch('app.agent.requests.post', side_effect=requests.exceptions.ConnectionError()):
            with self.assertRaisesRegex(ModelUnavailableError, 'Could not connect to Groq'):
                ModelAdapter().analyze_memory_message('I prefer Python')


if __name__ == "__main__":
    unittest.main()
