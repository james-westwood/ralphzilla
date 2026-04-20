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


class TestWaitAndFix:
    def test_first_try_passed(self, ci_poller, mock_runner):
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "success"})),
        ]

        task = {"id": "M1-17a", "title": "ci_poller_polling"}
        result = ci_poller.wait_and_fix(task, 42, "ralph/test-branch", {})

        assert result.passed is True

    def test_coder_fail_raises(self, ci_poller, mock_runner, mock_ai_runner):
        mock_runner.run.side_effect = [
            MagicMock(stdout="12345"),
            MagicMock(stdout=json.dumps({"status": "COMPLETED", "conclusion": "failure"})),
            MagicMock(stdout="log"),
        ]
        mock_ai_runner.assign_agents.return_value = ("claude", "gemini", "opencode")
        mock_ai_runner.run_coder.return_value = False

        task = {"id": "M1-17a", "title": "ci_poller_polling"}

        with pytest.raises(CIFailedFatal, match="Coder failed"):
            ci_poller.wait_and_fix(task, 42, "ralph/test-branch", {})
