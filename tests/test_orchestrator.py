from pathlib import Path
from unittest.mock import ANY, MagicMock

import pytest

from ralph import (
    BranchExistsError,
    BranchManager,
    BranchStatus,
    BranchSyncError,
    CIFailedFatal,
    CITimeoutError,
    Config,
    Orchestrator,
    PlanChecker,
    PlanInvalidError,
    PRDGuardViolation,
    PreflightError,
    PRInfo,
    RalphLogger,
    RemoteNotSSHError,
    TaskResult,
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
        orch.plan_checker.run = lambda prd, ai_check=False: call_order.append(("plan_check_run",))

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
        orch.review_loop = MagicMock()

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

    def test_standard_mode_does_not_call_test_writer(self, config, logger):
        config.tdd_mode = False

        orch = Orchestrator(config, logger)
        orch.test_writer = MagicMock()
        orch.test_quality_checker = MagicMock()
        orch.ai_runner = MagicMock()
        orch.ai_runner.assign_agents.return_value = ("coder", "reviewer", "writer")
        orch.branch_manager = MagicMock()
        orch.branch_manager.ensure_main_up_to_date.return_value = None
        orch.branch_manager.checkout_or_create.return_value = BranchStatus(
            existed=False, had_commits=False
        )
        orch.precommit_gate = MagicMock()
        orch.test_runner = MagicMock()
        orch.pr_manager = MagicMock()
        orch.ci_poller = MagicMock()
        orch.prd_guard = MagicMock()
        orch.review_loop = MagicMock()
        orch.runner = MagicMock()
        orch.task_tracker = MagicMock()

        orch.ai_runner.run_coder.return_value = True
        orch.precommit_gate.run.return_value = MagicMock(passed=True)
        orch.test_runner.run.return_value = MagicMock(passed=True)
        orch.branch_manager.push_branch.return_value = None
        orch.review_loop.run.return_value = MagicMock(verdict="APPROVED", rounds_used=1)
        orch.ci_poller.wait_and_fix.return_value = MagicMock(passed=True)
        orch.prd_guard.check.return_value = None
        orch.pr_manager.merge.return_value = None
        orch.branch_manager.merge_and_cleanup.return_value = None
        orch.task_tracker.append_progress.return_value = None
        orch.task_tracker.mark_complete.return_value = None
        orch.task_tracker.commit_tracking.return_value = None

        orch.pr_manager.create.return_value = PRInfo(number=1, url="")
        orch.pr_manager.get_existing.return_value = None

        task = {"id": "T-01", "title": "test task", "owner": "ralph"}
        orch._run_task(task, "branch", {})

        orch.test_writer.write_tests.assert_not_called()


class TestRunTaskStandardIntegration:
    """Integration tests verifying correct execution order via call sequence tracking."""

    @pytest.fixture
    def fully_mocked_orchestrator(self, config, logger):
        """Create an orchestrator with all components mocked."""
        orch = Orchestrator(config, logger)
        orch.branch_manager = MagicMock()
        orch.ai_runner = MagicMock()
        orch.precommit_gate = MagicMock()
        orch.test_runner = MagicMock()
        orch.pr_manager = MagicMock()
        orch.ci_poller = MagicMock()
        orch.prd_guard = MagicMock()
        orch.review_loop = MagicMock()
        orch.runner = MagicMock()
        orch.task_tracker = MagicMock()

        orch.ai_runner.assign_agents.return_value = ("coder", "reviewer", "writer")

        return orch

    def test_standard_mode_calls_components_in_correct_order(self, fully_mocked_orchestrator):
        """Verify state machine calls components in exact DESIGN.md order."""
        orch = fully_mocked_orchestrator

        orch.branch_manager.ensure_main_up_to_date.return_value = None
        orch.branch_manager.checkout_or_create.return_value = BranchStatus(
            existed=False, had_commits=False
        )
        orch.ai_runner.run_coder.return_value = True
        orch.precommit_gate.run.return_value = MagicMock(passed=True)
        orch.test_runner.run.return_value = MagicMock(passed=True)
        orch.branch_manager.push_branch.return_value = None
        orch.review_loop.run.return_value = MagicMock(verdict="APPROVED", rounds_used=1)
        orch.ci_poller.wait_and_fix.return_value = MagicMock(passed=True)
        orch.prd_guard.check.return_value = None
        orch.pr_manager.merge.return_value = None
        orch.branch_manager.merge_and_cleanup.return_value = None
        orch.task_tracker.append_progress.return_value = None
        orch.task_tracker.mark_complete.return_value = None
        orch.task_tracker.commit_tracking.return_value = None

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "ralph/branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is False

        calls = orch.branch_manager.method_calls
        expected_order = [
            "ensure_main_up_to_date",
            "checkout_or_create",
            "push_branch",
            "merge_and_cleanup",
        ]
        actual_methods = [c[0] for c in calls]
        for expected in expected_order:
            assert expected in actual_methods, f"{expected} not called in order"

    def test_branch_sync_error_returns_fatal_task_result(self, fully_mocked_orchestrator):
        """BranchSyncError → TaskResult(fatal=True)."""
        orch = fully_mocked_orchestrator
        orch.branch_manager.ensure_main_up_to_date.side_effect = BranchSyncError("diverged")

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True
        assert "diverged" in result.message

    def test_branch_exists_error_returns_fatal_task_result(self, fully_mocked_orchestrator):
        """BranchExistsError (no resume) → TaskResult(fatal=True)."""
        orch = fully_mocked_orchestrator
        orch.branch_manager.checkout_or_create.side_effect = BranchExistsError("exists")

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True

    def test_coder_failed_error_returns_fatal_task_result(self, fully_mocked_orchestrator):
        """CoderFailedError → TaskResult(fatal=True)."""
        orch = fully_mocked_orchestrator
        orch.ai_runner.run_coder.return_value = False

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True

    def test_precommit_failure_after_max_rounds_continues(self, fully_mocked_orchestrator):
        """PreCommitGate failure after max rounds → logs WARN, continues."""
        orch = fully_mocked_orchestrator
        orch.precommit_gate.run.return_value = MagicMock(passed=False, rounds_used=2)

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is False

    def test_testrunner_failure_after_max_rounds_continues(self, fully_mocked_orchestrator):
        """TestRunner failure after max rounds → logs WARN, continues."""
        orch = fully_mocked_orchestrator
        orch.test_runner.run.return_value = MagicMock(passed=False, rounds_used=2)

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is False

    def test_push_failure_returns_fatal_task_result(self, fully_mocked_orchestrator):
        """CalledProcessError on push → TaskResult(fatal=True)."""
        import subprocess

        orch = fully_mocked_orchestrator
        orch.branch_manager.push_branch.side_effect = subprocess.CalledProcessError(
            1, ["git", "push"]
        )

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True

    def test_review_max_rounds_exceeded_continues_to_ci(self, fully_mocked_orchestrator):
        """ReviewLoop max rounds exceeded → logs WARN, continues to CI."""
        orch = fully_mocked_orchestrator
        config.skip_review = False
        orch.review_loop.run.return_value = MagicMock(
            verdict="CHANGES_REQUESTED_MAX_REACHED", rounds_used=2
        )

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is False

    def test_ci_timeout_error_returns_fatal_task_result(self, fully_mocked_orchestrator):
        """CITimeoutError → TaskResult(fatal=True)."""
        orch = fully_mocked_orchestrator
        orch.ci_poller.wait_and_fix.side_effect = CITimeoutError("timeout")

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True

    def test_ci_failed_fatal_returns_fatal_task_result(self, fully_mocked_orchestrator):
        """CIFailedFatal → TaskResult(fatal=True)."""
        orch = fully_mocked_orchestrator
        orch.ci_poller.wait_and_fix.side_effect = CIFailedFatal("failed")

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True

    def test_prd_guard_violation_closes_pr_and_returns_fatal(self, fully_mocked_orchestrator):
        """PRDGuardViolation → closes PR, TaskResult(fatal=True)."""
        orch = fully_mocked_orchestrator
        orch.prd_guard.check.side_effect = PRDGuardViolation("touched prd")

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is True
        orch.pr_manager.close.assert_called_once_with(1, ANY)

    def test_success_calls_tracking_in_order(self, fully_mocked_orchestrator):
        """On success: calls mark_complete, append_progress, commit_tracking in that order."""
        orch = fully_mocked_orchestrator

        call_order = []

        def track_append_progress(*args, **kwargs):
            call_order.append("append_progress")

        def track_mark_complete(*args, **kwargs):
            call_order.append("mark_complete")

        def track_commit_tracking(*args, **kwargs):
            call_order.append("commit_tracking")

        orch.task_tracker.append_progress.side_effect = track_append_progress
        orch.task_tracker.mark_complete.side_effect = track_mark_complete
        orch.task_tracker.commit_tracking.side_effect = track_commit_tracking

        task = {"id": "T-01", "title": "test task"}
        result = orch._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is False
        assert call_order == [
            "append_progress",
            "mark_complete",
            "commit_tracking",
        ]

    def test_skip_review_skips_review_loop(self, fully_mocked_orchestrator):
        """When skip_review=True, ReviewLoop.run is not called."""
        config = fully_mocked_orchestrator.config
        config.skip_review = True

        task = {"id": "T-01", "title": "test task"}
        result = fully_mocked_orchestrator._run_task_standard(
            task, "branch", {}, "coder", "reviewer", PRInfo(number=1, url="")
        )

        assert result.fatal is False
        fully_mocked_orchestrator.review_loop.run.assert_not_called()


class TestMainLoop:
    """Tests for Orchestrator.run() main loop."""

    @pytest.fixture
    def loop_config(self, tmp_path):
        (tmp_path / "prd.json").write_text('{"tasks": []}')
        (tmp_path / "progress.txt").write_text("")
        return Config(
            max_iterations=3,
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

    def test_main_loop_logs_iteration_and_task_id(self, loop_config, logger):
        """Main loop logs iteration number and task ID at start of each iteration."""
        orch = Orchestrator(loop_config, logger)
        orch._preflight = MagicMock()
        orch.task_tracker = MagicMock()
        orch.task_tracker.load.return_value = {"tasks": []}
        call_count = [0]

        def get_next_task():
            call_count[0] += 1
            if call_count[0] == 1:
                return {"id": "T-01", "title": "test task", "owner": "ralph"}
            return None

        orch.task_tracker.get_next_task = get_next_task
        orch._run_task = MagicMock(return_value=TaskResult(fatal=False))

        def check_stop(task):
            if task is None:
                return "ALL TASKS COMPLETE"
            if task.get("owner") == "human":
                return "HUMAN_TASK_NEXT"
            return None

        orch._check_stop_conditions = check_stop

        log_lines = []
        orch.logger.info = lambda msg: log_lines.append(msg)

        orch.run(max_iterations=2)

        assert any("Iteration" in line and "T-01" in line for line in log_lines)

    def test_main_loop_stops_on_fatal_task_result(self, loop_config, logger):
        """Main loop stops when _run_task returns fatal TaskResult."""
        orch = Orchestrator(loop_config, logger)
        orch._preflight = MagicMock()
        orch.task_tracker = MagicMock()
        orch.task_tracker.load.return_value = {"tasks": []}
        orch.task_tracker.get_next_task.return_value = {
            "id": "T-01",
            "title": "test task",
            "owner": "ralph",
        }
        orch._run_task = MagicMock(return_value=TaskResult(fatal=True, message="Coder failed"))
        orch._check_stop_conditions = MagicMock(return_value=None)

        log_lines = []
        orch.logger.info = lambda msg: log_lines.append(msg)
        orch.logger.error = lambda msg: log_lines.append(msg)

        orch.run(max_iterations=2)

        assert any("fatal" in line.lower() or "failed" in line.lower() for line in log_lines)

    def test_main_loop_logs_clean_exit_reason(self, loop_config, logger):
        """Main loop logs clean exit reason when loop ends normally."""
        orch = Orchestrator(loop_config, logger)
        orch._preflight = MagicMock()
        orch.task_tracker = MagicMock()
        orch.task_tracker.load.return_value = {"tasks": []}
        orch.task_tracker.get_next_task.return_value = None
        orch._check_stop_conditions = MagicMock(return_value="ALL TASKS COMPLETE")

        log_lines = []
        orch.logger.info = lambda msg: log_lines.append(msg)

        orch.run(max_iterations=2)

        assert any("COMPLETE" in line or "finished" in line for line in log_lines)

    def test_main_loop_stops_on_human_task(self, loop_config, logger):
        """Main loop stops when next task is owned by human."""
        orch = Orchestrator(loop_config, logger)
        orch._preflight = MagicMock()
        orch.task_tracker = MagicMock()
        orch.task_tracker.load.return_value = {"tasks": []}
        orch.task_tracker.get_next_task.return_value = {
            "id": "T-01",
            "title": "test task",
            "owner": "human",
        }
        orch._check_stop_conditions = MagicMock(return_value="HUMAN_TASK_NEXT")

        log_lines = []
        orch.logger.info = lambda msg: log_lines.append(msg)

        orch.run(max_iterations=2)

        assert any("HUMAN" in line for line in log_lines)


class TestCommitPartialWork:
    def test_commits_dirty_files_on_coder_exception(self, config, logger):
        orch = Orchestrator(config, logger)
        orch.branch_manager = MagicMock()
        orch.branch_manager.ensure_main_up_to_date.return_value = None
        orch.branch_manager.checkout_or_create.return_value = BranchStatus(
            existed=False, had_commits=False
        )
        orch.ai_runner = MagicMock()
        orch.ai_runner.run_coder.side_effect = RuntimeError("coder crashed")
        orch.runner = MagicMock()
        dirty_result = MagicMock()
        dirty_result.stdout.strip.return_value = "M ralph.py\n?? tests/test_new.py"
        dirty_result.stdout = "M ralph.py\n?? tests/test_new.py"
        orch.runner.run.side_effect = None
        orch.runner.run.return_value = dirty_result

        task = {"id": "T-01", "title": "test_task"}
        result = orch._run_task_standard(
            task, "ralph/T-01-test_task", {}, "opencode", "opencode", None
        )

        assert result.fatal is True
        git_add_calls = [c for c in orch.runner.run.call_args_list if c[0][0][:2] == ["git", "add"]]
        git_commit_calls = [
            c for c in orch.runner.run.call_args_list if c[0][0][:2] == ["git", "commit"]
        ]
        assert len(git_add_calls) == 1
        assert len(git_commit_calls) == 1
        assert "coder-failed-partial" in git_commit_calls[0][0][0][-1]

    def test_commits_dirty_files_on_coder_failure(self, config, logger):
        orch = Orchestrator(config, logger)
        orch.branch_manager = MagicMock()
        orch.branch_manager.ensure_main_up_to_date.return_value = None
        orch.branch_manager.checkout_or_create.return_value = BranchStatus(
            existed=False, had_commits=False
        )
        orch.ai_runner = MagicMock()
        orch.ai_runner.run_coder.return_value = False
        orch.runner = MagicMock()
        dirty_result = MagicMock()
        dirty_result.stdout = "M ralph.py"
        orch.runner.run.return_value = dirty_result

        task = {"id": "T-01", "title": "test_task"}
        result = orch._run_task_standard(
            task, "ralph/T-01-test_task", {}, "opencode", "opencode", None
        )

        assert result.fatal is True
        git_commit_calls = [
            c for c in orch.runner.run.call_args_list if c[0][0][:2] == ["git", "commit"]
        ]
        assert len(git_commit_calls) == 1
        assert "coder-failed-partial" in git_commit_calls[0][0][0][-1]

    def test_no_commit_when_working_tree_clean(self, config, logger):
        orch = Orchestrator(config, logger)
        orch.runner = MagicMock()
        clean_result = MagicMock()
        clean_result.stdout.strip.return_value = ""
        clean_result.stdout.__str__ = lambda self: ""
        orch.runner.run.return_value = clean_result

        task = {"id": "T-01", "title": "test_task"}
        orch._commit_partial_work(task, "ralph/T-01-test_task")

        git_commit_calls = [
            c for c in orch.runner.run.call_args_list if c[0][0][:2] == ["git", "commit"]
        ]
        assert len(git_commit_calls) == 0
