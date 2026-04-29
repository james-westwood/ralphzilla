"""Tests for CIPoller (SHA-based CI monitoring)."""

from unittest.mock import MagicMock, patch

import pytest

from ralph import (
    AIRunner,
    CIFailedFatal,
    CIPoller,
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


class TestGetHeadSha:
    def test_returns_stripped_stdout(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(stdout="abc123def456\n")
        result = ci_poller._get_head_sha()
        assert result == "abc123def456"
        mock_runner.run.assert_called_once_with(
            ["git", "rev-parse", "HEAD"], check=True
        )


class TestGetGhToken:
    def test_returns_stripped_token(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(stdout="ghp_abc123\n")
        result = ci_poller._get_gh_token()
        assert result == "ghp_abc123"

    def test_raises_on_empty_token(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(stdout="\n")
        with pytest.raises(RuntimeError, match="gh auth token returned empty"):
            ci_poller._get_gh_token()


class TestGetRepoSlug:
    def test_ssh_url(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout="git@github.com:org/repo.git\n"
        )
        result = ci_poller._get_repo_slug()
        assert result == "org/repo"

    def test_https_url(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout="https://github.com/org/repo.git\n"
        )
        result = ci_poller._get_repo_slug()
        assert result == "org/repo"

    def test_unparseable_url_raises(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout="https://gitlab.com/org/repo.git\n"
        )
        with pytest.raises(RuntimeError, match="Cannot parse repo slug"):
            ci_poller._get_repo_slug()


class TestCiCheckSha:
    def test_no_workflow_runs(self, ci_poller):
        ci_poller._gh_api_get = MagicMock(return_value={"workflow_runs": []})
        result = ci_poller._ci_check_sha("abc123")
        assert result["status"] == "no_workflow"
        assert result["run_id"] is None

    def test_running_status(self, ci_poller):
        ci_poller._gh_api_get = MagicMock(
            return_value={
                "workflow_runs": [
                    {
                        "status": "in_progress",
                        "conclusion": None,
                        "id": 111,
                        "html_url": "https://github.com/...",
                    }
                ]
            }
        )
        result = ci_poller._ci_check_sha("abc123")
        assert result["status"] == "running"
        assert result["run_id"] == 111

    def test_passed_conclusion(self, ci_poller):
        ci_poller._gh_api_get = MagicMock(
            return_value={
                "workflow_runs": [
                    {
                        "status": "completed",
                        "conclusion": "success",
                        "id": 222,
                        "html_url": "https://github.com/...",
                    }
                ]
            }
        )
        result = ci_poller._ci_check_sha("abc123")
        assert result["status"] == "passed"

    def test_failed_conclusion(self, ci_poller):
        ci_poller._gh_api_get = MagicMock(
            return_value={
                "workflow_runs": [
                    {
                        "status": "completed",
                        "conclusion": "failure",
                        "id": 333,
                        "html_url": "https://github.com/...",
                    }
                ]
            }
        )
        result = ci_poller._ci_check_sha("abc123")
        assert result["status"] == "failed"

    def test_timed_out_conclusion(self, ci_poller):
        ci_poller._gh_api_get = MagicMock(
            return_value={
                "workflow_runs": [
                    {
                        "status": "completed",
                        "conclusion": "timed_out",
                        "id": 444,
                        "html_url": "https://github.com/...",
                    }
                ]
            }
        )
        result = ci_poller._ci_check_sha("abc123")
        assert result["status"] == "failed"

    def test_queued_status(self, ci_poller):
        ci_poller._gh_api_get = MagicMock(
            return_value={
                "workflow_runs": [
                    {
                        "status": "queued",
                        "conclusion": None,
                        "id": 555,
                        "html_url": "https://github.com/...",
                    }
                ]
            }
        )
        result = ci_poller._ci_check_sha("abc123")
        assert result["status"] == "running"

    def test_unknown_conclusion(self, ci_poller):
        ci_poller._gh_api_get = MagicMock(
            return_value={
                "workflow_runs": [
                    {
                        "status": "completed",
                        "conclusion": "weird",
                        "id": 666,
                        "html_url": "https://github.com/...",
                    }
                ]
            }
        )
        result = ci_poller._ci_check_sha("abc123")
        assert result["status"] == "unknown"


class TestCiWaitSha:
    def test_returns_immediately_on_passed(self, ci_poller):
        ci_poller._ci_check_sha = MagicMock(
            return_value={"status": "passed", "head_sha": "abc123", "run_id": 222}
        )
        result = ci_poller._ci_wait_sha("abc123")
        assert result["status"] == "passed"

    def test_returns_immediately_on_failed(self, ci_poller):
        ci_poller._ci_check_sha = MagicMock(
            return_value={"status": "failed", "head_sha": "abc123", "run_id": 333}
        )
        result = ci_poller._ci_wait_sha("abc123")
        assert result["status"] == "failed"

    def test_timeout_returns_timeout(self, ci_poller):
        ci_poller._ci_check_sha = MagicMock(
            return_value={"status": "running", "head_sha": "abc123", "run_id": 111}
        )
        with patch("ralph.time.time", side_effect=[0, 0, 0, 0, 999]):
            with patch("ralph.time.sleep"):
                result = ci_poller._ci_wait_sha("abc123", timeout=300)
        assert result["status"] == "timeout"


class TestCiFetchFailureLogs:
    def test_fetches_via_httpx(self, ci_poller):
        ci_poller._get_gh_token = MagicMock(return_value="ghp_test")
        ci_poller._get_repo_slug = MagicMock(return_value="org/repo")

        with patch("ralph.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = "error line 1\nerror line 2"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = ci_poller._ci_fetch_failure_logs(12345)

        assert "error line 1" in result
        mock_get.assert_called_once()

    def test_falls_back_to_gh_cli_on_exception(self, ci_poller):
        ci_poller._get_gh_token = MagicMock(return_value="ghp_test")
        ci_poller._get_repo_slug = MagicMock(return_value="org/repo")

        with patch("ralph.httpx.get", side_effect=Exception("network error")):
            ci_poller.runner.run.return_value = MagicMock(
                stdout="fallback log line 1\nfallback log line 2"
            )
            result = ci_poller._ci_fetch_failure_logs(12345)

        assert "fallback log line 1" in result

    def test_truncates_long_logs(self, ci_poller):
        ci_poller._get_gh_token = MagicMock(return_value="ghp_test")
        ci_poller._get_repo_slug = MagicMock(return_value="org/repo")

        long_log = "x" * 10000
        with patch("ralph.httpx.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.text = long_log
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp
            result = ci_poller._ci_fetch_failure_logs(12345)

        assert len(result) <= 4000


class TestWaitForCompletion:
    def test_passed(self, ci_poller):
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "passed", "head_sha": "abc123", "run_id": 222}
        )
        result = ci_poller.wait_for_completion(42, "feat/test")
        assert result.passed is True
        assert result.rounds_used == 1

    def test_failed(self, ci_poller):
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "failed", "head_sha": "abc123", "run_id": 333}
        )
        result = ci_poller.wait_for_completion(42, "feat/test")
        assert result.passed is False
        assert result.rounds_used == 1

    def test_no_workflow_returns_retryable(self, ci_poller):
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "no_workflow", "head_sha": "abc123", "run_id": None}
        )
        result = ci_poller.wait_for_completion(42, "feat/test")
        assert result.passed is False
        assert result.rounds_used == 0

    def test_timeout_returns_retryable(self, ci_poller):
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "timeout", "head_sha": "abc123", "run_id": 111}
        )
        result = ci_poller.wait_for_completion(42, "feat/test")
        assert result.passed is False
        assert result.rounds_used == 0


class TestCheckRequiredFailures:
    def test_required_check_fails_blocks(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout='[{"name":"build","conclusion":"failure","required":true},{"name":"lint","conclusion":"failure","required":true},{"name":"test-opt","conclusion":"failure","required":false}]'
        )
        has_required, failures = ci_poller._check_required_failures(42)
        assert has_required is True
        assert "build" in failures
        assert "lint" in failures
        assert "test-opt" not in failures

    def test_only_optional_fails_does_not_block(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(
            stdout='[{"name":"lint","conclusion":"success","required":true},{"name":"test-opt","conclusion":"failure","required":false}]'
        )
        has_required, failures = ci_poller._check_required_failures(42)
        assert has_required is False
        assert failures == []

    def test_no_checks_returns_pass(self, ci_poller, mock_runner):
        mock_runner.run.return_value = MagicMock(stdout="[]")
        has_required, failures = ci_poller._check_required_failures(42)
        assert has_required is False
        assert failures == []

    def test_exception_returns_fail_open(self, ci_poller, mock_runner):
        mock_runner.run.side_effect = Exception("network error")
        has_required, failures = ci_poller._check_required_failures(42)
        assert has_required is False
        assert failures == []


class TestWaitAndFix:
    def test_first_try_passed(self, ci_poller):
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "passed", "head_sha": "abc123", "run_id": 222}
        )
        ci_poller._check_required_failures = MagicMock(return_value=(False, []))
        task = {"id": "M1-17a", "title": "ci_poller_polling"}
        result = ci_poller.wait_and_fix(task, 42, "feat/test", {})
        assert result.passed is True

    def test_optional_check_failure_does_not_block(self, ci_poller):
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "passed", "head_sha": "abc123", "run_id": 222}
        )
        ci_poller._check_required_failures = MagicMock(return_value=(False, []))
        task = {"id": "M1-17b", "title": "ci_poller_fix_loop"}
        result = ci_poller.wait_and_fix(task, 42, "feat/test", {})
        assert result.passed is True

    def test_coder_fail_raises(self, ci_poller, mock_ai_runner):
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "failed", "head_sha": "abc123", "run_id": 333}
        )
        ci_poller._ci_check_sha = MagicMock(
            return_value={"status": "failed", "head_sha": "abc123", "run_id": 333}
        )
        ci_poller._ci_fetch_failure_logs = MagicMock(return_value="error log")
        mock_ai_runner.assign_agents.return_value = ("claude", "gemini", "opencode")
        mock_ai_runner.run_coder.return_value = False
        task = {"id": "M1-17a", "title": "ci_poller_polling"}
        with pytest.raises(CIFailedFatal, match="Coder failed"):
            ci_poller.wait_and_fix(task, 42, "feat/test", {})

    def test_fix_then_pass(self, ci_poller, mock_ai_runner):
        call_count = [0]

        def wait_sha_side_effect(sha):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"status": "failed", "head_sha": sha, "run_id": 333}
            return {"status": "passed", "head_sha": sha, "run_id": 444}

        ci_poller._get_head_sha = MagicMock(side_effect=["abc123", "def456"])
        ci_poller._ci_wait_sha = MagicMock(side_effect=wait_sha_side_effect)
        ci_poller._ci_check_sha = MagicMock(
            return_value={"status": "failed", "head_sha": "abc123", "run_id": 333}
        )
        ci_poller._ci_fetch_failure_logs = MagicMock(return_value="error log")
        ci_poller._check_required_failures = MagicMock(return_value=(False, []))
        mock_ai_runner.assign_agents.return_value = ("claude", "gemini", "opencode")
        mock_ai_runner.run_coder.return_value = True
        task = {"id": "M1-17b", "title": "ci_poller_fix_loop"}
        result = ci_poller.wait_and_fix(task, 42, "feat/test", {})
        assert result.passed is True
        assert result.rounds_used == 1

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
        ci_poller._get_head_sha = MagicMock(return_value="abc123")
        ci_poller._ci_wait_sha = MagicMock(
            return_value={"status": "failed", "head_sha": "abc123", "run_id": 333}
        )
        task = {"id": "M1-17a", "title": "ci_poller_polling"}
        with pytest.raises(CIFailedFatal, match="CI still failing"):
            ci_poller.wait_and_fix(task, 42, "feat/test", {})
