import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from ralph import AIRunner


def test_is_nested_claude_session():
    config = MagicMock()
    runner = MagicMock()
    logger = MagicMock()
    ai = AIRunner(runner, logger, config)

    with patch.dict(os.environ, {"CLAUDECODE": "1"}):
        assert ai._is_nested_claude_session() is True

    with patch.dict(os.environ, {}, clear=True):
        assert ai._is_nested_claude_session() is False


def test_assign_agents_overrides():
    config = MagicMock(claude_only=True, gemini_only=False, opencode_only=False)
    ai = AIRunner(MagicMock(), MagicMock(), config)
    assert ai.assign_agents({}) == ("claude", "claude")

    config.claude_only = False
    config.gemini_only = True
    assert ai.assign_agents({}) == ("gemini", "gemini")


def test_run_coder_claude():
    runner = MagicMock()
    config = MagicMock(opencode_model="kimi")
    ai = AIRunner(runner, MagicMock(), config)

    ai.run_coder("claude", "prompt", Path("."))

    runner.run.assert_called_with(
        ["claude", "--dangerously-skip-permissions", "--print", "prompt"],
        env_removals=["CLAUDECODE"],
        cwd=Path("."),
        check=True,
    )


def test_run_reviewer_nested_fallback():
    runner = MagicMock()
    runner.run.return_value.stdout = "Review content"
    config = MagicMock(opencode_model="kimi")
    ai = AIRunner(runner, MagicMock(), config)

    with patch.dict(os.environ, {"CLAUDECODE": "1"}):
        # Should fallback to gemini
        ai.run_reviewer("claude", "prompt")

    # Check if gemini was called instead
    # result = self.runner.run(["gemini", "-m", GEMINI_MODEL, "-p", prompt], ...)
    # The second call in the chain should be gemini
    assert runner.run.call_args[0][0][0] == "gemini"


def test_clean_output():
    ai = AIRunner(MagicMock(), MagicMock(), MagicMock())
    raw = "\x1b[31mError\x1b[0m\n> build starting\nValid output\n\u2713 Success"
    cleaned = ai._clean_output(raw)
    assert "Error" in cleaned
    assert "Valid output" in cleaned
    assert "> build" not in cleaned
    assert "\u2713" not in cleaned
