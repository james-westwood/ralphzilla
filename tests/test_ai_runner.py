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
    coder, rev, tw = ai.assign_agents({})
    assert (coder, rev) == ("claude", "claude")
    assert tw == "gemini"

    config.claude_only = False
    config.gemini_only = True
    coder, rev, tw = ai.assign_agents({})
    assert (coder, rev) == ("gemini", "claude")
    assert tw == "opencode"

    config.gemini_only = False
    config.opencode_only = True
    coder, rev, tw = ai.assign_agents({})
    assert (coder, rev) == ("opencode", "claude")
    assert tw == "gemini"


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


def test_assign_agents_complexity_routing():
    config = MagicMock(
        model_mode="random",
        claude_only=False,
        gemini_only=False,
        opencode_only=False,
    )
    ai = AIRunner(MagicMock(), MagicMock(), config)

    task_1 = {"complexity": 1}
    task_2 = {"complexity": 2}
    task_3 = {"complexity": 3}
    task_default = {}

    coder1, rev1, tw1 = ai.assign_agents(task_1)
    coder2, rev2, tw2 = ai.assign_agents(task_2)
    coder3, rev3, tw3 = ai.assign_agents(task_3)
    coder_def, rev_def, tw_def = ai.assign_agents(task_default)

    assert coder1 == "opencode", f"complexity 1 should use opencode/kimi, got {coder1}"
    assert rev1 == "gemini", f"complexity 1 should use gemini for reviewer, got {rev1}"
    assert tw1 != coder1, "test_writer must be different from coder"

    assert coder2 == "gemini", f"complexity 2 should use gemini, got {coder2}"
    assert rev2 == "claude", f"complexity 2 should use claude for reviewer, got {rev2}"
    assert tw2 != coder2, "test_writer must be different from coder"

    assert coder3 == "claude", f"complexity 3 should use claude, got {coder3}"
    assert rev3 == "gemini", f"complexity 3 should use gemini for reviewer, got {rev3}"
    assert tw3 != coder3, "test_writer must be different from coder"

    assert coder_def == "opencode", f"default complexity should use opencode, got {coder_def}"
    assert tw_def != coder_def, "test_writer must be different from coder"


def test_run_reviewer_claude_removes_claudecode():
    runner = MagicMock()
    runner.run.return_value.stdout = "Review output"
    config = MagicMock(opencode_model="kimi")
    ai = AIRunner(runner, MagicMock(), config)

    with patch.dict(os.environ, {}, clear=True):
        ai.run_reviewer("claude", "test prompt")

    call_kwargs = runner.run.call_args[1]
    assert call_kwargs.get("env_removals") == ["CLAUDECODE"]


def test_run_test_writer_uses_different_model():
    runner = MagicMock()
    runner.run.return_value.stdout = "Tests written"
    config = MagicMock(opencode_model="kimi")
    logger = MagicMock()
    ai = AIRunner(runner, logger, config)

    ai.run_test_writer("test prompt", Path("."))

    call_args = runner.run.call_args[0][0]
    assert call_args[0] in ("claude", "gemini")
