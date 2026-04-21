"""Tests for CIPoller."""

import json
from unittest.mock import MagicMock, patch

import pytest

import ralph
from ralph import (
    AIRunner,
    CIFailedFatal,
    CIPoller,
    CITimeoutError,
    Config,
    RalphLogger,
    SubprocessRunner,
)


@pytest.fixture
def mock_runner():
    return MagicMock(spec=SubprocessRunner)


@pytest.fixture
def mock_ai_runner():
    return MagicMock(spec=AIRunner)


@pytest.fixture
def mock_logger():
    return MagicMock(spec=RalphLogger)


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
        log_file=tmp_path / "test.log",
        max_precommit_rounds=2,
        max_review_rounds=2,
        max_ci_fix_rounds=2,
        max_test_fix_rounds=2,
        max_test_write_rounds=2,
        force_task_id=None,
        deep_review_check=False,
    )


@pytest.fixture
def ci_poller(mock_runner, mock_ai_runner, mock_logger, config):
    return CIPoller(mock_runner, mock_ai_runner, mock_logger, config)


class TestGetLatestRunId:
    def test_success(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(stdout="12345")

        result = ci_poller._get_latest_run_id("ralph/test-branch")

        assert result == "12345"
        mock_runner.run.assert_called_once()

    def test_empty_raises_timeout(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(stdout="")

        with pytest.raises(CITimeoutError, match="No run found"):
            ci_poller._get_latest_run_id("ralph/test-branch")


class TestWaitForRun:
    def test_pending_then_success(self, ci_poller, mock_runner):
        with patch.object(ralph, "CI_POLL_MAX_ATTEMPTS", 2):
            with patch.object(ralph, "CI_POLL_INTERVAL_SECS", 0):
                mock_runner.run.side_effect = [
                    MagicMock(stdout=json.dumps({"status": "IN_PROGRESS", "conclusion": None})),
                    MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "success"})),
                ]

                result = ci_poller._wait_for_run("12345")

                assert result == "PASSED"

    def test_failure(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout=json.dumps({"status": "COMPLETED", "conclusion": "failure"})
        )

        result = ci_poller._wait_for_run("12345")

        assert result == "failure"

    def test_timeout(self, ci_poller, mock_runner):
        with patch.object(ralph, "CI_POLL_MAX_ATTEMPTS", 1):
            with patch.object(ralph, "CI_POLL_INTERVAL_SECS", 0):
                mock_runner.run.return_value = MagicMock(
                    stdout=json.dumps({"status": "IN_PROGRESS", "conclusion": None})
                )

                with pytest.raises(CITimeoutError, match="did not complete"):
                    ci_poller._wait_for_run("12345")


class TestWaitForNewRun:
    def test_new_run_appears(self, ci_poller, mock_runner):
        call_count = [0]
        original_time = ralph.time.time

        def fake_time():
            call_count[0] += 1
            return call_count[0] * 5

        ralph.time.time = fake_time

        try:
            mock_runner.run.side_effect = [
                MagicMock(stdout="12345"),
                MagicMock(stdout="67890"),
            ]

            result = ci_poller._wait_for_new_run("ralph/test", "12345", timeout=20)

            assert result == "67890"
        finally:
            ralph.time.time = original_time


class TestWaitForCompletion:
    def test_passed(self, ci_poller, mock_runner):
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "success"})),
        ]

        result = ci_poller.wait_for_completion(42, "ralph/test-branch")

        assert result.passed is True
        assert result.rounds_used == 1

    def test_failed(self, ci_poller, mock_runner):
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "failure"})),
        ]

        result = ci_poller.wait_for_completion(42, "ralph/test-branch")

        assert result.passed is False
        assert result.rounds_used == 1


class TestCheckRequiredFailures:
    def test_required_check_fails_blocks(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout=json.dumps(
                [
                    {"name": "build", "conclusion": "failure", "required": True},
                    {"name": "lint", "conclusion": "failure", "required": True},
                    {"name": "test-optional", "conclusion": "failure", "required": False},
                ]
            )
        )

        has_required, failures = ci_poller._check_required_failures(42)

        assert has_required is True
        assert "build" in failures
        assert "lint" in failures
        assert "test-optional" not in failures

    def test_only_optional_fails_does_not_block(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout=json.dumps(
                [
                    {"name": "lint", "conclusion": "success", "required": True},
                    {"name": "test-optional", "conclusion": "failure", "required": False},
                ]
            )
        )

        has_required, failures = ci_poller._check_required_failures(42)

        assert has_required is False
        assert failures == []

    def test_no_checks_returns_pass(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(stdout=json.dumps([]))

        has_required, failures = ci_poller._check_required_failures(42)

        assert has_required is False
        assert failures == []


class TestWaitAndFix:
    def test_first_try_passed(self, ci_poller, mock_runner):
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "success"})),
            MagicMock(stdout=json.dumps([])),  # _check_required_failures -> no checks
        ]

        task = {"id": "M1-17a", "title": "ci_poller_polling"}
        result = ci_poller.wait_and_fix(task, 42, "ralph/test-branch", {})

        assert result.passed is True

    def test_optional_check_failure_does_not_block(self, ci_poller, mock_runner):
        """CI passes and only optional checks fail — returns success without invoking fix."""
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "success"})),
            MagicMock(
                stdout=json.dumps([{"name": "lint", "conclusion": "failure", "required": False}])
            ),
        ]

        task = {"id": "M1-17b", "title": "ci_poller_fix_loop"}
        result = ci_poller.wait_and_fix(task, 42, "ralph/test-branch", {})

        assert result.passed is True

    def test_coder_fail_raises(self, ci_poller, mock_runner, mock_ai_runner):
        # CI failure path does NOT call _check_required_failures
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "failure"})),
            MagicMock(stdout="failure log"),  # gh run view --log-failed
        ]
        mock_ai_runner.assign_agents.return_value = ("claude", "gemini", "opencode")
        mock_ai_runner.run_coder.return_value = False

        task = {"id": "M1-17a", "title": "ci_poller_polling"}

        with pytest.raises(CIFailedFatal, match="Coder failed"):
            ci_poller.wait_and_fix(task, 42, "ralph/test-branch", {})


class TestWaitForNewRunAfterPush:
    def test_wait_for_new_run_called_after_push(self, tmp_path):
        """_wait_for_new_run is called after push_branch with the pre-push run ID."""
        config = Config(
            max_iterations=10,
            skip_review=False,
            tdd_mode=False,
            model_mode="random",
            opencode_model="opencode/kimi-k2.5",
            resume=False,
            repo_dir=tmp_path,
            log_file=tmp_path / "test.log",
            max_precommit_rounds=2,
            max_review_rounds=2,
            max_ci_fix_rounds=2,
            max_test_fix_rounds=2,
            max_test_write_rounds=2,
            force_task_id=None,
            deep_review_check=False,
        )
        mock_runner = MagicMock(spec=SubprocessRunner)
        mock_ai_runner = MagicMock(spec=AIRunner)
        mock_logger = MagicMock(spec=RalphLogger)
        ci_poller = CIPoller(mock_runner, mock_ai_runner, mock_logger, config)

        ci_poller._wait_for_new_run = MagicMock(return_value="67890")

        mock_runner.run.side_effect = [
            # Round 1: CI fails (no _check_required_failures call on CI failure)
            MagicMock(stdout="12345"),  # get_run_id in wait_for_completion
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "failure"})),
            MagicMock(stdout="failure output"),  # gh run view --log-failed
            MagicMock(stdout="12345"),  # get_run_id before push (current_run_id)
            # push_branch: verify_ssh_remote + git push
            MagicMock(stdout="git@github.com:org/repo.git"),
            MagicMock(stdout=""),
            # Round 2: CI passes — _check_required_failures IS called now
            MagicMock(stdout="67890"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "success"})),
            MagicMock(stdout=json.dumps([])),  # _check_required_failures -> no failures
        ]
        mock_ai_runner.assign_agents.return_value = ("claude", "gemini", "opencode")
        mock_ai_runner.run_coder.return_value = True

        task = {"id": "M1-17b", "title": "ci_poller_fix_loop"}
        result = ci_poller.wait_and_fix(task, 42, "ralph/test-branch", {})

        assert result.passed is True
        ci_poller._wait_for_new_run.assert_called_once_with(
            "ralph/test-branch", "12345", timeout=180
        )

    def test_max_rounds_raises_cifailedfatal(self, tmp_path):
        config = Config(
            max_iterations=10,
            skip_review=False,
            tdd_mode=False,
            model_mode="random",
            opencode_model="opencode/kimi-k2.5",
            resume=False,
            repo_dir=tmp_path,
            log_file=tmp_path / "test.log",
            max_precommit_rounds=2,
            max_review_rounds=2,
            max_ci_fix_rounds=1,
            max_test_fix_rounds=2,
            max_test_write_rounds=2,
            force_task_id=None,
            deep_review_check=False,
        )
        mock_runner = MagicMock(spec=SubprocessRunner)
        mock_ai_runner = MagicMock(spec=AIRunner)
        mock_logger = MagicMock(spec=RalphLogger)
        ci_poller = CIPoller(mock_runner, mock_ai_runner, mock_logger, config)

        # CI failure path: _check_required_failures not called, only 2 mocks needed
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "failure"})),
        ]

        task = {"id": "M1-17a", "title": "ci_poller_polling"}

        with pytest.raises(CIFailedFatal, match="CI still failing"):
            ci_poller.wait_and_fix(task, 42, "ralph/test-branch", {})
