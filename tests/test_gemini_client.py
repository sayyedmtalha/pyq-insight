import os
import unittest
from unittest.mock import patch

import gemini_client


class GeminiClientTests(unittest.TestCase):
    def setUp(self):
        gemini_client._GEMINI_CLIENT = None
        gemini_client._GEMINI_CLIENTS = []
        gemini_client._GEMINI_CLIENT_INDEX = 0
        gemini_client._GEMINI_DISABLED = False

    def test_get_gemini_client_reuses_a_single_instance(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False):
            fake_client = object()
            with patch("gemini_client.genai.Client", return_value=fake_client) as mock_client:
                first = gemini_client.get_gemini_client()
                second = gemini_client.get_gemini_client()

                self.assertIs(first, second)
                self.assertEqual(mock_client.call_count, 1)

    def test_rotate_gemini_client_switches_to_next_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEYS": "key-1,key-2"}, clear=False):
            first_client = object()
            second_client = object()
            gemini_client._GEMINI_CLIENTS = []
            gemini_client._GEMINI_CLIENT_INDEX = 0

            with patch("gemini_client.genai.Client", side_effect=[first_client, second_client]) as mock_client:
                first = gemini_client.get_gemini_client()
                self.assertIs(first, first_client)

                gemini_client._rotate_gemini_client()
                second = gemini_client.get_gemini_client()

                self.assertIs(second, second_client)
                self.assertEqual(mock_client.call_count, 2)

    def test_quota_failure_disables_gemini_for_future_calls(self):
        class FakeModels:
            def __init__(self, should_raise):
                self.should_raise = should_raise

            def generate_content(self, *args, **kwargs):
                raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")

        class FakeClient:
            def __init__(self, should_raise):
                self.models = FakeModels(should_raise)

        first_client = FakeClient(True)
        second_client = FakeClient(True)
        gemini_client._GEMINI_CLIENTS = [first_client, second_client]
        gemini_client._GEMINI_CLIENT_INDEX = 0
        gemini_client._GEMINI_CLIENT = first_client

        with self.assertRaises(RuntimeError):
            gemini_client.generate_structured(["dummy"], object)

        self.assertTrue(gemini_client._GEMINI_DISABLED)


if __name__ == "__main__":
    unittest.main()
