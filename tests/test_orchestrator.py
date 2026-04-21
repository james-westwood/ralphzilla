from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ralph import (
    BranchExistsError,
    BranchStatus,
    BranchSyncError,
    Config,
    Orchestrator,
    PreflightError,
    PRInfo,
    RalphLogger,
)


def make_orchestrator_mocks():
    mocks = [
        patch("ralph.TaskTracker"),
        patch("ralph.BranchManager"),
        patch("ralph.PRManager"),
        patch("ralph.AIRunner"),
        patch("ralph.PRDGuard"),
        patch("ralph.PreCommitGate"),
        patch("ralph.TestRunner"),
        patch("ralph.TestWriter"),
        patch("ralph.TestQualityChecker"),
        patch("ralph.ReviewLoop"),
        patch("ralph.CIPoller"),
        patch("ralph.PlanChecker"),
    ]
    for m in mocks:
        m.start()
    return mocks


def stop_all_mocks(mocks):
    for m in reversed(mocks):
        m.stop()


@pytest.fixture
def config(tmp_path):
    return Config(
        max_iterations=10,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model="opencode/kimi-k2.5",
        resume=False,
        repo_dir=tmp_path,
        log_file=tmp_path / "ralph.log",
        max_precommit_rounds=2,
        max_review_rounds=2,
        max_ci_fix_rounds=2,
        max_test_fix_rounds=2,
        max_test_write_rounds=2,
        force_task_id=None,
    )


@pytest.fixture
def logger(tmp_path):
    return RalphLogger(tmp_path / "ralph.log")


class TestCheckStopConditions:
    def test_no_task_returns_all_complete(self, config, logger):
        mocks = make_orchestrator_mocks()
        orch = Orchestrator(config, logger)
        assert orch._check_stop_conditions(None) == "ALL TASKS COMPLETE"
        stop_all_mocks(mocks)

    def test_human_task_returns_human_next(self, config, logger):
        mocks = make_orchestrator_mocks()
        orch = Orchestrator(config, logger)
        assert orch._check_stop_conditions({"id": "T-01", "owner": "human"}) == "HUMAN_TASK_NEXT"
        stop_all_mocks(mocks)

    def test_ralph_task_returns_none(self, config, logger):
        mocks = make_orchestrator_mocks()
        orch = Orchestrator(config, logger)
        assert orch._check_stop_conditions({"id": "T-01", "owner": "ralph"}) is None
        stop_all_mocks(mocks)


class TestCheckCli:
    def test_check_cli_returns_true_when_found(self, config, logger):
        mocks = make_orchestrator_mocks()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock()
            orch = Orchestrator(config, logger)
            assert orch._check_cli("ls") is True
        stop_all_mocks(mocks)

    def test_check_cli_returns_false_when_not_found(self, config, logger):
        mocks = make_orchestrator_mocks()
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError()
            orch = Orchestrator(config, logger)
            result = orch._check_cli("nonexistent")
            assert result is False
        stop_all_mocks(mocks)


class TestPreflight:
    def test_preflight_raises_on_missing_gh(self, config, logger):
        mocks = make_orchestrator_mocks()
        with patch.object(Orchestrator, "_check_cli", return_value=False):
            orch = Orchestrator(config, logger)
            with pytest.raises(PreflightError, match="gh CLI not found"):
                orch._preflight({})
        stop_all_mocks(mocks)

    def test_human_task_returns_human_next(self, config, logger):
        with make_orchestrator_mocks() as mocks:
            orch = Orchestrator(config, logger)
            assert (
                orch._check_stop_conditions({"id": "T-01", "owner": "human"}) == "HUMAN_TASK_NEXT"
            )
            stop_all_mocks(mocks)

    def test_ralph_task_returns_none(self, config, logger):
        with make_orchestrator_mocks() as mocks:
            orch = Orchestrator(config, logger)
            assert orch._check_stop_conditions({"id": "T-01", "owner": "ralph"}) is None
            stop_all_mocks(mocks)


class TestCheckCli:
    def test_check_cli_returns_true_when_found(self, config, logger):
        with make_orchestrator_mocks() as mocks:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock()
                orch = Orchestrator(config, logger)
                assert orch._check_cli("ls") is True
            stop_all_mocks(mocks)

    def test_check_cli_returns_false_when_not_found(self, config, logger):
        with make_orchestrator_mocks() as mocks:
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = FileNotFoundError()
                orch = Orchestrator(config, logger)
                assert orch._check_cli("nonexistent") is False
            stop_all_mocks(mocks)


class TestPreflight:
    def test_preflight_raises_on_missing_gh(self, config, logger):
        with make_orchestrator_mocks() as mocks:
            with patch.object(Orchestrator, "_check_cli") as mock_cli:
                mock_cli.return_value = False
                orch = Orchestrator(config, logger)
                with pytest.raises(PreflightError, match="gh CLI not found"):
                    orch._preflight({})
            stop_all_mocks(mocks)


class TestRunTaskStandard:
    def test_branch_sync_error_is_fatal(self, config, logger):
        mocks = make_orchestrator_mocks()
        try:
            orch = Orchestrator(config, logger)
            orch.branch_manager = MagicMock()
            orch.branch_manager.ensure_main_up_to_date.side_effect = BranchSyncError("diverged")
            orch.branch_manager.checkout_or_create.return_value = BranchStatus(
                existed=False, had_commits=False
            )

            task = {"id": "T-01", "title": "test task"}
            result = orch._run_task_standard(
                task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
            )

            assert result.fatal is True
        finally:
            stop_all_mocks(mocks)

    def test_branch_exists_error_is_fatal(self, config, logger):
        mocks = make_orchestrator_mocks()
        try:
            orch = Orchestrator(config, logger)
            orch.branch_manager = MagicMock()
            orch.branch_manager.ensure_main_up_to_date.return_value = None
            orch.branch_manager.checkout_or_create.side_effect = BranchExistsError("exists")

            task = {"id": "T-01", "title": "test task"}
            result = orch._run_task_standard(
                task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
            )

            assert result.fatal is True
        finally:
            stop_all_mocks(mocks)


class TestRunTaskTdd:
    def test_calls_test_writer_before_coder_in_tdd(self, config, logger):
        config.tdd_mode = True
        mocks = make_orchestrator_mocks()
        try:
            orch = Orchestrator(config, logger)

            orch.test_writer = MagicMock()
            orch.test_quality_checker = MagicMock()
            orch.ai_runner = MagicMock()
            orch.ai_runner.assign_agents.return_value = ("coder", "reviewer", "writer")
            orch.branch_manager = MagicMock()
            orch.precommit_gate = MagicMock()
            orch.test_runner = MagicMock()
            orch.pr_manager = MagicMock()
            orch.ci_poller = MagicMock()
            orch.prd_guard = MagicMock()

            orch.test_writer.write_tests.return_value = Path("tests/test_task.py")
            orch.test_quality_checker.run.return_value = MagicMock(passed=True)

            orch.pr_manager.create.return_value = PRInfo(number=1, url="")
            orch.pr_manager.get_existing.return_value = None

            task = {"id": "T-01", "title": "test task"}
            _ = orch._run_task(task, "branch", {})

            orch.test_writer.write_tests.assert_called_once()
        finally:
            stop_all_mocks(mocks)

    def test_fails_on_test_quality_failure(self, config, logger):
        config.tdd_mode = True
        mocks = make_orchestrator_mocks()
        try:
            orch = Orchestrator(config, logger)

            orch.test_writer = MagicMock()
            orch.test_quality_checker = MagicMock()
            orch.ai_runner = MagicMock()
            orch.ai_runner.assign_agents.return_value = ("coder", "reviewer", "writer")
            orch.branch_manager = MagicMock()
            orch.precommit_gate = MagicMock()
            orch.test_runner = MagicMock()
            orch.pr_manager = MagicMock()
            orch.ci_poller = MagicMock()
            orch.prd_guard = MagicMock()

            orch.test_writer.write_tests.return_value = Path("tests/test_task.py")
            failing = MagicMock(passed=False)
            orch.test_quality_checker.run.return_value = failing

            orch.pr_manager.create.return_value = PRInfo(number=1, url="")
            orch.pr_manager.get_existing.return_value = None

            task = {"id": "T-01", "title": "test task"}
            result = orch._run_task(task, "branch", {})

            assert result.fatal is True
        finally:
            stop_all_mocks(mocks)
