"""Offline probe checks. No provider tokens or external requests."""
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.tumblr_ai_probe import run


class RecordingTransport:
    def __init__(self, status=200, payload=None, error=None):
        self.urls = []
        self.status = status
        self.payload = payload
        self.error = error

    def post(self, url, headers, body):
        self.urls.append(url)
        if self.error:
            raise self.error
        if "generativelanguage" in url:
            text = json.dumps(self.payload)
            return self.status, {"candidates": [{"content": {"parts": [{"text": text}]}}]}
        return self.status, {"choices": [{"message": {"content": json.dumps(self.payload)}}]}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = str(Path(temp.name) / "quota.sqlite3")

    def env(self, **extra):
        base = {
            "TUMBLR_AI_LIVE_ENABLED": "1",
            "TUMBLR_AI_QUOTA_DB": self.db,
            "TUMBLR_GEMINI_API_KEY": "example-gemini",
            "TUMBLR_GROQ_API_KEY": "example-groq",
            "TUMBLR_OPENROUTER_API_KEY": "example-openrouter",
        }
        base.update(extra)
        return base

    def capture(self, env, transport):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = run(env, transport=transport, sleep=lambda _seconds: None)
        return code, buffer.getvalue()

    def test_development_calls_only_openrouter_free(self):
        transport = RecordingTransport(payload={"reply": "ok"})
        code, output = self.capture(self.env(), transport)
        self.assertEqual(code, 0)
        self.assertIn("ROUTING=free_router", output)
        self.assertIn("PROVIDERS=openrouter", output)
        self.assertIn("CALL provider=openrouter result=ok", output)
        self.assertEqual(len(transport.urls), 1)
        self.assertIn("openrouter.ai", transport.urls[0])
        self.assertNotIn("example-openrouter", output)
        self.assertEqual(stat.S_IMODE(os.stat(self.db).st_mode), 0o600)

    def test_legacy_switch_calls_gemini_and_groq_only(self):
        transport = RecordingTransport(payload={"reply": "ok"})
        code, output = self.capture(self.env(TUMBLR_AI_ROUTING_MODE="legacy_split"), transport)
        self.assertEqual(code, 0)
        self.assertIn("CALL provider=gemini result=ok", output)
        self.assertIn("CALL provider=groq result=ok", output)
        self.assertNotIn("provider=openrouter", output)
        self.assertEqual(len(transport.urls), 2)
        self.assertNotIn("example-gemini", output)
        self.assertNotIn("example-groq", output)

    def test_gemini_status_can_be_checked_without_calling_groq(self):
        transport = RecordingTransport(status=404, payload={"reply": "ok"})
        code, output = self.capture(
            self.env(TUMBLR_AI_ROUTING_MODE="legacy_split", TUMBLR_AI_PROBE_ONLY="gemini"),
            transport,
        )
        self.assertEqual(code, 1)
        self.assertIn("CALL provider=gemini result=failed http=404", output)
        self.assertNotIn("provider=groq", output)
        self.assertEqual(transport.urls, ["https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"])

    def test_provider_exception_text_is_not_printed(self):
        transport = RecordingTransport(error=RuntimeError("example-openrouter leaked"))
        code, output = self.capture(self.env(), transport)
        self.assertEqual(code, 1)
        self.assertIn("result=error", output)
        self.assertNotIn("example-openrouter", output)
        self.assertNotIn("leaked", output)


if __name__ == "__main__":
    unittest.main()
