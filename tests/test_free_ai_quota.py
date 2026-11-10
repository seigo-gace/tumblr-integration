"""Offline-only regression checks. No provider tokens or external requests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tumblrkit.assistant import DurableFreeQuota, ProviderError, Assistant, ScriptedProvider


class DurableFreeQuotaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "quota.sqlite3")

    def quota(self, enabled=True, path=True):
        return DurableFreeQuota({
            "TUMBLR_AI_LIVE_ENABLED": "1" if enabled else "0",
            "TUMBLR_AI_QUOTA_DB": self.db if path else "",
        })

    def assert_code(self, func, code):
        with self.assertRaises(ProviderError) as error:
            func()
        self.assertEqual(error.exception.code, code)

    def test_missing_enable_or_persistent_store_fails_closed(self):
        self.assert_code(lambda: self.quota(enabled=False).reserve("groq"), "ai_disabled")
        self.assert_code(lambda: self.quota(path=False).reserve("groq"), "ai_disabled")

    def test_persists_across_restart_and_limits_each_provider(self):
        for i in range(5):
            with patch("time.time", return_value=86400 * 100 + i * 61):
                self.quota().reserve("gemini")
        with patch("time.time", return_value=86400 * 100 + 400):
            self.assert_code(lambda: self.quota().reserve("gemini"), "quota_exhausted")
        # Different provider has an independent cap, but shares the same cooldown.
        with patch("time.time", return_value=86400 * 100 + 500):
            self.quota().reserve("groq")

    def test_global_limit_and_no_reset_on_process_recreation(self):
        for i in range(5):
            with patch("time.time", return_value=86400 * 100 + i * 61):
                self.quota().reserve("gemini")
        for i in range(5):
            with patch("time.time", return_value=86400 * 100 + 400 + i * 61):
                self.quota().reserve("groq")
        with patch("time.time", return_value=86400 * 100 + 800):
            self.assert_code(lambda: self.quota().reserve("openrouter"), "quota_exhausted")

    def test_cooldown_does_not_consume_extra_reservation(self):
        with patch("time.time", return_value=86400 * 100 + 10):
            self.quota().reserve("groq")
        with patch("time.time", return_value=86400 * 100 + 20):
            self.assert_code(lambda: self.quota().reserve("gemini"), "quota_cooldown")
        with patch("time.time", return_value=86400 * 100 + 71):
            self.quota().reserve("gemini")

    def test_unwritable_quota_path_fails_closed(self):
        gate = DurableFreeQuota({
            "TUMBLR_AI_LIVE_ENABLED": "1",
            "TUMBLR_AI_QUOTA_DB": self.db + "/missing.sqlite3",
        })
        self.assert_code(lambda: gate.reserve("groq"), "quota_unavailable")

    def test_429_does_not_try_another_provider(self):
        primary = ScriptedProvider("groq", error="rate_limited")
        fallback = ScriptedProvider("openrouter", {"reply": "would consume more"}, paid=False)
        result = Assistant(providers=[primary, fallback]).turn("短文")
        self.assertEqual(result["state"], "rate_limited")
        self.assertEqual(len(primary.prompts), 1)
        self.assertEqual(len(fallback.prompts), 0)

    def test_disabled_provider_explains_zero_calls(self):
        primary = ScriptedProvider("groq", error="ai_disabled")
        fallback = ScriptedProvider("openrouter", {"reply": "unexpected"}, paid=False)
        result = Assistant(providers=[primary, fallback]).turn("短文")
        self.assertEqual(result["state"], "ai_disabled")
        self.assertEqual(len(fallback.prompts), 0)
        self.assertFalse(result["executed"])


if __name__ == "__main__":
    unittest.main()
