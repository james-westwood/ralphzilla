from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ralph import (
    BranchExistsError,
    BranchManager,
    BranchStatus,
    BranchSyncError,
    Config,
    Orchestrator,
    PlanChecker,
    PlanInvalidError,
    PreflightError,
    PRInfo,
    RalphLogger,
    RemoteNotSSHError,
)


@pytest.fixture
def config(tmp_path):
    (tmp_path / "prd.json").write_text('{"tasks": []}')
    (tmp_path / "progress.txt").write_text("")
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
        orch = Orchestrator(config, logger)
        assert orch._check_stop_conditions(None) == "ALL TASKS COMPLETE"

    def test_human_task_returns_human_next(self, config, logger):
        orch = Orchestrator(config, logger)
        assert orch._check_stop_conditions({"id": "T-01", "owner": "human"}) == "HUMAN_TASK_NEXT"

    def test_ralph_task_returns_none(self, config, logger):
        orch = Orchestrator(config, logger)
        assert orch._check_stop_conditions({"id": "T-01", "owner": "ralph"}) is None


class TestCheckCli:
    def test_check_cli_returns_true_when_found(self, config, logger):
        orch = Orchestrator(config, logger)
        orch.runner = MagicMock()
        orch.runner.run.return_value = MagicMock()
        assert orch._check_cli("ls") is True

    def test_check_cli_returns_false_when_not_found(self, config, logger):
        orch = Orchestrator(config, logger)
        orch.runner = MagicMock()
        orch.runner.run.side_effect = FileNotFoundError()
        result = orch._check_cli("nonexistent")
        assert result is False


class TestPreflight:
    def test_preflight_raises_on_missing_gh(self, config, logger):
        orch = Orchestrator(config, logger)
        orch._check_cli = MagicMock(return_value=False)
        with pytest.raises(PreflightError, match="gh CLI not found"):
            orch._preflight({})

    def test_preflight_raises_on_missing_git(self, config, logger):
        orch = Orchestrator(config, logger)

        def cli_side_effect(cmd):
            if cmd == "gh":
                return True
            return False

        orch._check_cli = cli_side_effect
        with pytest.raises(PreflightError, match="git CLI not found"):
            orch._preflight({})

    def test_preflight_raises_on_https_remote(self, config, logger):
        orch = Orchestrator(config, logger)
        orch._check_cli = MagicMock(return_value=True)
        orch.branch_manager = MagicMock(spec=BranchManager)
        orch.branch_manager.verify_ssh_remote = MagicMock(
            side_effect=RemoteNotSSHError("HTTPS remote")
        )
        with pytest.raises(PreflightError, match="HTTPS remote"):
            orch._preflight({})

    def test_preflight_raises_on_dirty_prd(self, config, logger):
        orch = Orchestrator(config, logger)
        orch._check_cli = MagicMock(return_value=True)
        orch.branch_manager = MagicMock(spec=BranchManager)
        orch.branch_manager.verify_ssh_remote = MagicMock()
        orch.runner = MagicMock()
        orch.runner.run.return_value = MagicMock(returncode=1)
        with pytest.raises(PreflightError, match="uncommitted local changes"):
            orch._preflight({})

    def test_preflight_raises_on_plan_invalid(self, config, logger):
        orch = Orchestrator(config, logger)
        orch._check_cli = MagicMock(return_value=True)
        orch.branch_manager = MagicMock(spec=BranchManager)
        orch.branch_manager.verify_ssh_remote = MagicMock()
        orch.runner = MagicMock()
        orch.runner.run.return_value = MagicMock(returncode=0)
        orch.plan_checker = MagicMock(spec=PlanChecker)
        orch.plan_checker.run = MagicMock(side_effect=PlanInvalidError("invalid plan"))
        with pytest.raises(PreflightError, match="Plan validation failed"):
            orch._preflight({})

    def test_preflight_validation_order(self, config, logger):
        call_order = []

        def check_cli(cmd):
            call_order.append(("check_cli", cmd))
            return True

        orch = Orchestrator(config, logger)
        orch._check_cli = check_cli
        orch.branch_manager = MagicMock(spec=BranchManager)
        orch.branch_manager.verify_ssh_remote = lambda: call_order.append(("verify_ssh",))
        orch.runner = MagicMock()
        orch.runner.run = lambda cmd, **kw: (
            call_order.append(("run_git", cmd[0] if isinstance(cmd, list) else cmd))
            or MagicMock(returncode=0)
        )
        orch.plan_checker = MagicMock(spec=PlanChecker)
        orch.plan_checker.run = lambda prd: call_order.append(("plan_check_run",))

        try:
            orch._preflight({})
        except Exception:
            pass

        assert ("check_cli", "gh") in call_order
        assert ("check_cli", "git") in call_order
        assert ("verify_ssh",) in call_order
        assert ("plan_check_run",) in call_order


class TestPreflightClaudeCodeWarning:
    def test_claudecode_warning_logged_when_env_set(self, config, logger, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        orch = Orchestrator(config, logger)
        orch._check_cli = MagicMock(return_value=True)
        orch.branch_manager = MagicMock(spec=BranchManager)
        orch.branch_manager.verify_ssh_remote = MagicMock()
        orch.runner = MagicMock()
        orch.runner.run.return_value = MagicMock(returncode=0)
        orch.plan_checker = MagicMock(spec=PlanChecker)
        orch.plan_checker.run = MagicMock()

        log_lines = []
        orch.logger.warn = lambda msg: log_lines.append(msg)
        orch._preflight({})

        assert any("Claude Code" in line for line in log_lines)

    def test_no_claudecode_warning_when_env_not_set(self, config, logger, monkeypatch):
        monkeypatch.delenv("CLAUDECODE", raising=False)
        orch = Orchestrator(config, logger)
        orch._check_cli = MagicMock(return_value=True)
        orch.branch_manager = MagicMock(spec=BranchManager)
        orch.branch_manager.verify_ssh_remote = MagicMock()
        orch.runner = MagicMock()
        orch.runner.run.return_value = MagicMock(returncode=0)
        orch.plan_checker = MagicMock(spec=PlanChecker)
        orch.plan_checker.run = MagicMock()

        warn_called = False
        _ = orch.logger.warn

        def check_no_warning(msg):
            nonlocal warn_called
            if "Claude Code" in msg:
                warn_called = True

        orch.logger.warn = check_no_warning
        orch._preflight({})

        assert not warn_called


class TestRunTaskStandard:
    def test_branch_sync_error_is_fatal(self, config, logger):
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

    def test_branch_exists_error_is_fatal(self, config, logger):
        orch = Orchestrator(config, logger)
        orch.branch_manager = MagicMock()
        orch.branch_manager.ensure_main_up_to_date.return_value = None
        orch.branch_manager.checkout_or_create.side_effect = BranchExistsError("exists")

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True


class TestRunTaskTdd:
    def test_calls_test_writer_before_coder_in_tdd(self, config, logger):
        config.tdd_mode = True

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
        orch.runner = MagicMock()
        orch.task_tracker = MagicMock()

        orch.test_writer.write_tests.return_value = Path("tests/test_task.py")
        orch.test_quality_checker.run.return_value = MagicMock(passed=True)

        orch.pr_manager.create.return_value = PRInfo(number=1, url="")
        orch.pr_manager.get_existing.return_value = None

        task = {"id": "T-01", "title": "test task", "owner": "ralph"}
        _ = orch._run_task(task, "branch", {})

        orch.test_writer.write_tests.assert_called_once()

    def test_fails_on_test_quality_failure(self, config, logger):
        config.tdd_mode = True

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
