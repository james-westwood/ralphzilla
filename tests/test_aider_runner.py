import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from ralph import (
    AiderRunner,
    AIRunnerBase,
    RalphLogger,
    RuntimeConfig,
    SubprocessRunner,
)


def test_aider_runner_is_airunner_base():
    """AiderRunner extends AIRunnerBase."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)
    aider = AiderRunner(runner, logger, config)
    assert isinstance(aider, AIRunnerBase)


def test_aider_runner_detects_cli_available(monkeypatch):
    """AiderRunner detects aider CLI availability."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "aider 0.50.0"
    runner.run.return_value = mock_result

    aider = AiderRunner(runner, logger, config)
    assert aider.is_available() is True


def test_aider_runner_detects_cli_unavailable(monkeypatch):
    """AiderRunner detects when aider CLI is not available."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    runner.run.side_effect = FileNotFoundError("aider not found")

    aider = AiderRunner(runner, logger, config)
    assert aider.is_available() is False


def test_aider_runner_get_available_runtimes():
    """get_available_runtimes returns {'aider'} when available."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "aider 0.50.0"
    runner.run.return_value = mock_result

    aider = AiderRunner(runner, logger, config)
    assert aider.get_available_runtimes() == {"aider"}


def test_aider_runner_run_task_builds_correct_command():
    """run_task() executes aider with correct arguments."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)
    config.repo_dir = Path("/repo")

    with patch("ralph.TaskTracker") as mock_tracker_class:
        mock_tracker = MagicMock()
        mock_tracker_class.return_value = mock_tracker
        mock_tracker.get_task_by_id.return_value = {
            "id": "M1-01",
            "title": "Test task",
            "description": "A test task",
            "acceptance_criteria": ["AC1"],
            "files": ["ralph.py"],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Modified: ralph.py\nTask complete"
        mock_result.stderr = ""
        runner.run.return_value = mock_result

        aider = AiderRunner(runner, logger, config)
        aider.run_task("M1-01", "feature-M1-01")

    call_args = runner.run.call_args
    cmd = call_args.args[0]
    assert cmd[0] == "aider"
    assert "--no-auto-commits" in cmd
    assert "--no-git" in cmd
    assert "--yes" in cmd
    assert "--message" in cmd
    assert "--file" in cmd


def test_aider_runner_run_task_with_model():
    """run_task() passes --model flag when aider_model is set."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)
    config.aider_model = "gpt-4"

    with patch("ralph.TaskTracker") as mock_tracker_class:
        mock_tracker = MagicMock()
        mock_tracker_class.return_value = mock_tracker
        mock_tracker.get_task_by_id.return_value = {
            "id": "M1-01",
            "title": "Test",
            "description": "Test",
            "acceptance_criteria": [],
            "files": [],
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Done"
        mock_result.stderr = ""
        runner.run.return_value = mock_result

        aider = AiderRunner(runner, logger, config)
        aider.run_task("M1-01", "feature-M1-01")

    call_args = runner.run.call_args
    cmd = call_args.args[0]
    assert "--model" in cmd
    model_idx = cmd.index("--model")
    assert cmd[model_idx + 1] == "gpt-4"


def test_aider_runner_timeout_handling():
    """Timeout handling prevents indefinite hangs."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=10)

    # Mock is_available to return True
    with patch.object(AiderRunner, "is_available", return_value=True):
        runner.run.side_effect = subprocess.TimeoutExpired(cmd=["aider"], timeout=10)

        with patch("ralph.TaskTracker") as mock_tracker_class:
            mock_tracker = MagicMock()
            mock_tracker_class.return_value = mock_tracker
            mock_tracker.get_task_by_id.return_value = {
                "id": "M1-01",
                "title": "Test",
                "description": "Test",
                "acceptance_criteria": [],
                "files": [],
            }
            aider = AiderRunner(runner, logger, config)
            result = aider.run_task("M1-01", "feature-M1-01")

    assert result.success is False
    assert result.output == "TIMEOUT"


def test_aider_runner_output_capture():
    """Output capture includes file modification summaries."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = """Added: tests/test_new.py
Modified: ralph.py
Committed: Added new feature
"""
    mock_result.stderr = ""
    runner.run.return_value = mock_result

    with patch("ralph.TaskTracker") as mock_tracker_class:
        mock_tracker = MagicMock()
        mock_tracker_class.return_value = mock_tracker
        mock_tracker.get_task_by_id.return_value = {
            "id": "M1-01",
            "title": "Test",
            "description": "Test",
            "acceptance_criteria": [],
            "files": [],
        }
        aider = AiderRunner(runner, logger, config)
        result = aider.run_task("M1-01", "feature-M1-01")

    assert result.success is True
    assert "Added: tests/test_new.py" in result.output
    assert "Modified: ralph.py" in result.output


def test_aider_runner_task_not_found():
    """run_task returns failure when task_id is not found."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    # Mock is_available to return True
    with patch.object(AiderRunner, "is_available", return_value=True):
        with patch("ralph.TaskTracker") as mock_tracker_class:
            mock_tracker = MagicMock()
            mock_tracker_class.return_value = mock_tracker
            mock_tracker.get_task_by_id.return_value = None
            aider = AiderRunner(runner, logger, config)
            result = aider.run_task("NONEXISTENT", "feature-nonexistent")

    assert result.success is False
    assert result.task_id == "NONEXISTENT"


def test_aider_runner_start_new_session():
    """run_task uses start_new_session to prevent input prompts."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "Done"
    runner.run.return_value = mock_result

    with patch("ralph.TaskTracker") as mock_tracker_class:
        mock_tracker = MagicMock()
        mock_tracker_class.return_value = mock_tracker
        mock_tracker.get_task_by_id.return_value = {
            "id": "M1-01",
            "title": "Test",
            "description": "Test",
            "acceptance_criteria": [],
            "files": [],
        }
        aider = AiderRunner(runner, logger, config)
        aider.run_task("M1-01", "feature-M1-01")

    call_kwargs = runner.run.call_args.kwargs
    assert call_kwargs.get("start_new_session") is True


def test_aider_runner_check_version():
    """check_version returns version string when aider is available."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "aider 0.50.0"
    runner.run.return_value = mock_result

    aider = AiderRunner(runner, logger, config)
    version = aider.check_version("aider")
    assert version is not None
    assert "0.50.0" in version


def test_aider_runner_check_version_unsupported_runtime():
    """check_version returns None for non-aider runtime."""
    runner = MagicMock(spec=SubprocessRunner)
    logger = MagicMock(spec=RalphLogger)
    config = RuntimeConfig(primary="aider", timeout=600)

    aider = AiderRunner(runner, logger, config)
    version = aider.check_version("claude")
    assert version is None
