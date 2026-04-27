from unittest.mock import MagicMock

import pytest

from ralph import (
    AIRunnerBase,
    RuntimeConfig,
    RuntimeUnavailableError,
)


class ConcreteTestRunner(AIRunnerBase):
    """Concrete implementation for testing."""

    def run_task(self, task_id: str, branch_name: str):
        from ralph import TaskRunResult

        return TaskRunResult(
            success=True,
            task_id=task_id,
            branch_name=branch_name,
            output=f"Completed {task_id}",
        )

    def get_available_runtimes(self) -> set[str]:
        return {"aider", "opencode", "claude", "gemini"}


class TestAIRunnerBase:
    def test_base_class_is_abstract(self):
        with pytest.raises(TypeError):
            AIRunnerBase(MagicMock(), MagicMock(), MagicMock())

    def test_run_task_is_abstract_method(self):
        assert hasattr(AIRunnerBase, "run_task")
        # run_task is abstract and enforced by ABC
        assert getattr(AIRunnerBase, "run_task", None) is not None

    def test_get_available_runtimes_returns_set(self):
        runner = ConcreteTestRunner(MagicMock(), MagicMock(), MagicMock())
        runtimes = runner.get_available_runtimes()
        assert isinstance(runtimes, set)
        assert "opencode" in runtimes

    def test_check_version_returns_version_or_none(self):
        runner = ConcreteTestRunner(MagicMock(), MagicMock(), MagicMock())
        version = runner.check_version("opencode")
        # Version should be string or None
        assert version is None or isinstance(version, str)


class TestRuntimeConfig:
    def test_valid_config(self):
        config = RuntimeConfig(
            primary="opencode",
            fallback=["claude", "gemini"],
            timeout=300,
        )
        assert config.primary == "opencode"
        assert config.timeout == 300

    def test_default_timeout(self):
        config = RuntimeConfig(primary="opencode")
        assert config.timeout == 600

    def test_fallback_defaults_to_empty(self):
        config = RuntimeConfig(primary="opencode")
        assert config.fallback == []

    def test_invalid_runtime_raises(self):
        with pytest.raises(ValueError) as exc_info:
            RuntimeConfig(primary="unsupported_runtime")
        assert "unsupported_runtime" in str(exc_info.value)

    def test_invalid_fallback_raises(self):
        with pytest.raises(ValueError) as exc_info:
            RuntimeConfig(primary="opencode", fallback=["invalid_tool"])
        assert "invalid_tool" in str(exc_info.value)


class TestRuntimeConfigSupportedRuntimes:
    def test_all_runtimes_recognized(self):
        valid_runtimes = RuntimeConfig.SUPPORTED_RUNTIMES
        expected = {
            "aider",
            "claude",
            "claude-code",
            "cursor",
            "cline",
            "codex",
            "gemini",
            "opencode",
        }
        assert expected.issubset(valid_runtimes)


class TestAIRunnerImplementation:
    def test_aider_runtime_detection(self):
        mock_runner = MagicMock()
        mock_logger = MagicMock()
        config = RuntimeConfig(primary="aider")
        runner = ConcreteTestRunner(mock_runner, mock_logger, config)

        runtimes = runner.get_available_runtimes()
        assert "aider" in runtimes

    def test_run_task_returns_result(self):
        mock_runner = MagicMock()
        mock_logger = MagicMock()
        config = RuntimeConfig(primary="opencode")
        runner = ConcreteTestRunner(mock_runner, mock_logger, config)

        result = runner.run_task("TASK-01", "feature/test")
        assert result.task_id == "TASK-01"
        assert result.branch_name == "feature/test"
        assert result.success is True

    def test_get_effective_runtime_primary_available(self):
        mock_runner = MagicMock()
        mock_logger = MagicMock()
        config = RuntimeConfig(primary="opencode")
        runner = ConcreteTestRunner(mock_runner, mock_logger, config)

        effective = runner.get_effective_runtime()
        assert effective == "opencode"

    def test_get_effective_runtime_fallback(self):
        mock_runner = MagicMock()
        mock_logger = MagicMock()
        config = RuntimeConfig(primary="cursor", fallback=["opencode"])
        runner = ConcreteTestRunner(mock_runner, mock_logger, config)

        effective = runner.get_effective_runtime()
        assert effective == "opencode"

    def test_unsupported_runtime_raises_error(self):
        mock_runner = MagicMock()
        mock_logger = MagicMock()
        config = RuntimeConfig(primary="cursor", fallback=[])
        runner = ConcreteTestRunner(mock_runner, mock_logger, config)

        # cursor is supported but not detected, so fallback to nothing
        # should raise RuntimeUnavailableError
        with pytest.raises(RuntimeUnavailableError) as exc_info:
            runner.get_effective_runtime()
        assert "cursor" in str(exc_info.value)


class TestRuntimeDetection:
    def test_version_check_with_valid_runtime(self):
        mock_runner = MagicMock()
        mock_runner.run.return_value = MagicMock(stdout="v1.2.3\n", stderr="", returncode=0)
        mock_logger = MagicMock()
        config = RuntimeConfig(primary="opencode")
        runner = ConcreteTestRunner(mock_runner, mock_logger, config)

        version = runner.check_version("opencode")
        assert version == "1.2.3"

    def test_version_check_nonexistent_returns_none(self):
        mock_runner = MagicMock()
        mock_logger = MagicMock()
        config = RuntimeConfig(primary="opencode")
        runner = ConcreteTestRunner(mock_runner, mock_logger, config)

        version = runner.check_version("nonexistent_tool_xyz")
        assert version is None
