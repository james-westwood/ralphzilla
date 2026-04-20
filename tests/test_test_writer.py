from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ralph import RalphError, RalphTestWriter


def test_test_writer_constructor():
    """RalphTestWriter accepts ai_runner, runner, and logger in constructor."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()

    tw = RalphTestWriter(ai_runner, runner, logger)

    assert tw.ai_runner is ai_runner
    assert tw.runner is runner
    assert tw.logger is logger


def test_write_tests_invokes_test_writer():
    """write_tests invokes AIRunner.run_test_writer with test_writer_prompt."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()
    ai_runner.assign_agents.return_value = ("opencode", "gemini", "claude")

    task = {"id": "TASK-01", "title": "my task", "description": "desc", "acceptance_criteria": []}
    branch_dir = Path("/fake/repo")

    tw = RalphTestWriter(ai_runner, runner, logger)
    with patch.object(tw, "_discover_test_file", return_value=Path("tests/test_my_task.py")):
        tw.write_tests(task, branch_dir)

    ai_runner.assign_agents.assert_called_once_with(task)
    ai_runner.run_test_writer.assert_called_once()


def test_write_tests_commits_test_file():
    """write_tests commits the test file to the current branch."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()
    ai_runner.assign_agents.return_value = ("opencode", "gemini", "claude")

    task = {
        "id": "TASK-01",
        "title": "my task",
        "description": "desc",
        "acceptance_criteria": ["AC1"],
    }
    branch_dir = Path("/fake/repo")

    test_file_path = Path("tests/test_my_task.py")

    tw = RalphTestWriter(ai_runner, runner, logger)
    with patch.object(tw, "_discover_test_file", return_value=test_file_path):
        tw.write_tests(task, branch_dir)

    calls = runner.run.call_args_list
    add_call = [c for c in calls if "git" in c[0][0] and "add" in c[0][0]][0]
    assert str(test_file_path) in add_call[0][0]

    commit_call = [c for c in calls if "git" in c[0][0] and "commit" in c[0][0]][0]
    assert "[TASK-01] my task: add failing tests" in commit_call[0][0]


def test_write_tests_returns_test_file_path():
    """write_tests returns the Path to the committed test file."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()
    ai_runner.assign_agents.return_value = ("opencode", "gemini", "claude")

    task = {"id": "TASK-02", "title": "foo bar", "description": "desc", "acceptance_criteria": []}
    branch_dir = Path("/fake/repo")

    test_file_path = Path("tests/test_foo_bar.py")

    tw = RalphTestWriter(ai_runner, runner, logger)
    with patch.object(tw, "_discover_test_file", return_value=test_file_path):
        result = tw.write_tests(task, branch_dir)

    assert result == test_file_path


def test_discover_test_file_pattern(tmp_path):
    """_discover_test_file looks for test_{task_title}*.py in tests/."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()

    task = {"title": "Implement Feature X"}

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_implement_feature_x.py"
    test_file.write_text("# test")

    tw = RalphTestWriter(ai_runner, runner, logger)
    result = tw._discover_test_file(task, tmp_path)

    assert result.name == "test_implement_feature_x.py"


def test_discover_test_file_not_found(tmp_path):
    """_discover_test_file raises error when no test file found."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()

    task = {"title": "Some Task"}

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()

    tw = RalphTestWriter(ai_runner, runner, logger)

    with pytest.raises(RalphError, match="No test file found"):
        tw._discover_test_file(task, tmp_path)


def test_discover_test_file_no_tests_dir(tmp_path):
    """_discover_test_file raises error when tests/ directory doesn't exist."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()

    task = {"title": "Some Task"}
    branch_dir = tmp_path

    tw = RalphTestWriter(ai_runner, runner, logger)

    with pytest.raises(RalphError, match="tests/ directory not found"):
        tw._discover_test_file(task, branch_dir)


def test_write_tests_uses_test_writer_from_assign_agents():
    """The test writer model comes from assign_agents (different from coder)."""
    ai_runner = MagicMock()
    runner = MagicMock()
    logger = MagicMock()
    ai_runner.assign_agents.return_value = ("claude", "gemini", "opencode")

    task = {"id": "TEST-01", "title": "test", "description": "desc", "acceptance_criteria": []}
    branch_dir = Path("/fake/repo")

    tw = RalphTestWriter(ai_runner, runner, logger)
    with patch.object(tw, "_discover_test_file", return_value=Path("tests/test_x.py")):
        tw.write_tests(task, branch_dir)

    ai_runner.run_test_writer.assert_called_once()
    args, kwargs = ai_runner.run_test_writer.call_args
    assert kwargs.get("agent") == "opencode"


def test_all_three_agents_are_distinct():
    """assign_agents returns three distinct models."""
    config = MagicMock(
        model_mode="random",
        claude_only=False,
        gemini_only=False,
        opencode_only=False,
    )
    from ralph import AIRunner

    ai = AIRunner(MagicMock(), MagicMock(), config)

    for complexity in [1, 2, 3]:
        task = {"complexity": complexity}
        coder, reviewer, test_writer = ai.assign_agents(task)
        assert coder != reviewer, f"coder ({coder}) must differ from reviewer ({reviewer})"
        assert coder != test_writer, f"coder ({coder}) must differ from test_writer ({test_writer})"
        assert reviewer != test_writer, (
            f"reviewer ({reviewer}) must differ from test_writer ({test_writer})"
        )
