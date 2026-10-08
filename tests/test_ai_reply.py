import json
import os
import subprocess
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
                json.dump(
                    {
                        "r1": {"reply": "¡Gracias!", "auto_reply": True, "ping": False},
                        "r2": {"reply": "Sorry to hear that.", "auto_reply": False, "ping": True},
                    },
                    fh,
                )

            class R:  # minimal CompletedProcess stand-in
                pass

            return R()

        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=fake_run) as run:
            result = generate_suggested_replies(REVIEWS)

        self.assertEqual(
            result,
            {
                "r1": {"reply": "¡Gracias!", "auto_reply": True, "ping": False},
                "r2": {"reply": "Sorry to hear that.", "auto_reply": False, "ping": True},
            },
        )
        # Correct codex invocation.
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--sandbox", cmd)

    def test_bare_string_output_is_tolerated_as_reply_with_flags_false(self):
        def fake_run(cmd, **kwargs):
            with open(os.path.join(kwargs["cwd"], "suggested_replies.json"), "w", encoding="utf-8") as fh:
                json.dump({"r1": "Thanks!"}, fh)  # old id->string shape
            return None

        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=fake_run):
            result = generate_suggested_replies(REVIEWS)

        self.assertEqual(result["r1"], {"reply": "Thanks!", "auto_reply": False, "ping": False})

    def test_non_bool_flags_are_coerced(self):
        def fake_run(cmd, **kwargs):
            with open(os.path.join(kwargs["cwd"], "suggested_replies.json"), "w", encoding="utf-8") as fh:
                json.dump({"r1": {"reply": "Hi", "auto_reply": 1, "ping": "yes"}}, fh)
            return None

        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=fake_run):
            result = generate_suggested_replies(REVIEWS)

        self.assertIs(result["r1"]["auto_reply"], True)
        self.assertIs(result["r1"]["ping"], True)

    def test_codex_failure_returns_empty(self):
        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=RuntimeError("codex died")):
            self.assertEqual(generate_suggested_replies(REVIEWS), {})

    def test_codex_nonzero_exit_logs_stderr_not_prompt(self):
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["codex", "exec"],
            output="You write official public developer replies ...",  # prompt echo
            stderr="ERROR codex_login::auth::manager: token_revoked",
        )
        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=error):
            with self.assertLogs("common.ai_reply", level="WARNING") as logs:
                self.assertEqual(generate_suggested_replies(REVIEWS), {})
        logged = "\n".join(logs.output)
        # The real cause (stderr) is surfaced; the prompt echo on stdout is not.
        self.assertIn("token_revoked", logged)
        self.assertNotIn("official public developer replies", logged)

    def test_codex_stderr_is_deduplicated(self):
        # Same message repeated with different timestamps -> collapsed to one (xN).
        stderr = "\n".join(
            f"2026-10-06T23:57:3{i}.000000Z ERROR auth::manager: refresh token was revoked"
            for i in range(5)
        )
        error = subprocess.CalledProcessError(returncode=1, cmd=["codex"], output="", stderr=stderr)
        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=error):
            with self.assertLogs("common.ai_reply", level="WARNING") as logs:
                self.assertEqual(generate_suggested_replies(REVIEWS), {})
        logged = "\n".join(logs.output)
        self.assertIn("(x5) ERROR auth::manager: refresh token was revoked", logged)
        # The message appears once (as the deduped line), not five times.
        self.assertEqual(logged.count("refresh token was revoked"), 1)

    def test_codex_timeout_logs_captured_stderr(self):
        timeout = subprocess.TimeoutExpired(
            cmd=["codex", "exec"], timeout=600, output="", stderr="hung waiting on model"
        )
        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=timeout):
            with self.assertLogs("common.ai_reply", level="WARNING") as logs:
                self.assertEqual(generate_suggested_replies(REVIEWS), {})
        self.assertIn("hung waiting on model", "\n".join(logs.output))

    def test_missing_output_file_returns_empty(self):
        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", return_value=None):
            # subprocess "succeeded" but wrote no file -> open() raises -> {}
            self.assertEqual(generate_suggested_replies(REVIEWS), {})

    def test_overlong_and_blank_replies_are_clamped_and_dropped(self):
        def fake_run(cmd, **kwargs):
            with open(os.path.join(kwargs["cwd"], "suggested_replies.json"), "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "r1": {"reply": "x" * 600, "auto_reply": False, "ping": False},
                        "r2": {"reply": "   "},  # blank reply
                        "r3": 5,  # non-dict, non-string
                        "r4": {"auto_reply": True},  # missing reply
                    },
                    fh,
                )
            return None

        with patch.object(ai_reply, "_codex_available", return_value=True), \
             patch("common.ai_reply.subprocess.run", side_effect=fake_run):
            result = generate_suggested_replies(REVIEWS)

        self.assertEqual(len(result["r1"]["reply"]), MAX_SUGGESTED_REPLY_LENGTH)
        self.assertNotIn("r2", result)  # blank reply dropped
        self.assertNotIn("r3", result)  # non-dict dropped
        self.assertNotIn("r4", result)  # missing reply dropped


if __name__ == "__main__":
    unittest.main()
