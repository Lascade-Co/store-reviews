import json
import os
import unittest
from unittest.mock import patch

from common import ai_reply
from common.ai_reply import MAX_SUGGESTED_REPLY_LENGTH, generate_suggested_replies


REVIEWS = [
    {"id": "r1", "platform": "Google Play", "rating": 5, "title": "", "body": "buena"},
    {"id": "r2", "platform": "Apple App Store", "rating": 2, "title": "Bad", "body": "too costly"},
]


class GenerateSuggestedRepliesTests(unittest.TestCase):
    def test_empty_input_returns_empty_without_codex(self):
        with patch.object(ai_reply, "_codex_available", return_value=True) as avail:
            self.assertEqual(generate_suggested_replies([]), {})
            avail.assert_not_called()

    def test_missing_codex_auth_skips(self):
        with patch.object(ai_reply, "_codex_available", return_value=False):
            with patch("common.ai_reply.subprocess.run") as run:
                self.assertEqual(generate_suggested_replies(REVIEWS), {})
                run.assert_not_called()

    def test_reads_codex_output_file(self):
        def fake_run(cmd, **kwargs):
            # Codex writes the output file into its cwd (the scratch dir).
            with open(os.path.join(kwargs["cwd"], "suggested_replies.json"), "w", encoding="utf-8") as fh:
                json.dump({"r1": "¡Gracias!", "r2": "Sorry to hear that."}, fh)

            class R:  # minimal CompletedProcess stand-in
                pass

            return R()

        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=fake_run) as run:
            result = generate_suggested_replies(REVIEWS)

        self.assertEqual(result, {"r1": "¡Gracias!", "r2": "Sorry to hear that."})
        # Correct codex invocation.
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--sandbox", cmd)

    def test_codex_failure_returns_empty(self):
        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=RuntimeError("codex died")):
            self.assertEqual(generate_suggested_replies(REVIEWS), {})

    def test_missing_output_file_returns_empty(self):
        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", return_value=None):
            # subprocess "succeeded" but wrote no file -> open() raises -> {}
            self.assertEqual(generate_suggested_replies(REVIEWS), {})

    def test_overlong_and_blank_replies_are_clamped_and_dropped(self):
        def fake_run(cmd, **kwargs):
            with open(os.path.join(kwargs["cwd"], "suggested_replies.json"), "w", encoding="utf-8") as fh:
                json.dump({"r1": "x" * 600, "r2": "   ", "r3": 5}, fh)
            return None

        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=fake_run):
            result = generate_suggested_replies(REVIEWS)

        self.assertEqual(len(result["r1"]), MAX_SUGGESTED_REPLY_LENGTH)
        self.assertNotIn("r2", result)  # blank dropped
        self.assertNotIn("r3", result)  # non-string dropped


if __name__ == "__main__":
    unittest.main()
