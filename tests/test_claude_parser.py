from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "lib" / "legs" / "ask_claude.sh"


def extract_served(payload: dict, requested: str = "") -> subprocess.CompletedProcess[str]:
    args = [str(SCRIPT), "--extract-served-model"]
    if requested:
        args.append(requested)
    return subprocess.run(
        args,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(ROOT),
        timeout=5,
    )


class ClaudeServedModelParserTests(unittest.TestCase):
    def test_requested_alias_wins_over_auxiliary_haiku(self):
        payload = {
            "result": "ok",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {"outputTokens": 500},
                "claude-opus-4-8": {"outputTokens": 43},
            },
        }

        result = extract_served(payload, "opus")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "claude-opus-4-8")

    def test_falls_back_to_dominant_model_without_request_match(self):
        payload = {
            "result": "ok",
            "modelUsage": {
                "claude-haiku-4-5-20251001": {"outputTokens": 5},
                "claude-opus-4-8": {"outputTokens": 900},
            },
        }

        result = extract_served(payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "claude-opus-4-8")

    def test_fails_closed_when_model_usage_missing(self):
        result = extract_served({"result": "ok"})

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
