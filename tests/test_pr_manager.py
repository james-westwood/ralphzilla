"""Tests for PRManager."""

import json
from unittest.mock import MagicMock

import pytest

from playchitect import PRInfo, PRManager
from ralph import RalphLogger, SubprocessRunner


@pytest.fixture
def mock_logger(tmp_path):
    log_file = tmp_path / "ralph.log"
    return RalphLogger(log_file)


@pytest.fixture
def mock_runner():
    return MagicMock(spec=SubprocessRunner)


class TestPRManagerCreate:
    """Tests for PRManager.create()."""

    def test_create_parses_pr_number_with_regex(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/123",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.create("feature-branch", "Test PR", "Test body")

        assert result.number == 123
        assert result.url == "https://github.com/owner/repo/pull/123"

    def test_create_parses_pr_number_with_higher_digits(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="https://github.com/owner/repo/pull/4567",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.create("feature-branch", "Test PR", "Test body")

        assert result.number == 4567

    def test_create_raises_on_failure(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: fork denied",
        )

        pr_manager = PRManager(mock_runner, mock_logger)

        with pytest.raises(Exception, match="fork denied"):
            pr_manager.create("feature-branch", "Test PR", "Test body")

    def test_create_raises_on_unparseable_output(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="not a valid URL",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)

        with pytest.raises(Exception, match="Could not parse PR number"):
            pr_manager.create("feature-branch", "Test PR", "Test body")


class TestPRManagerGetExisting:
    """Tests for PRManager.get_existing()."""

    def test_get_existing_returns_pr_info(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{"number": 42, "url": "https://github.com/owner/repo/pull/42"}]),
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_existing("feature-branch")

        assert result is not None
        assert result.number == 42
        assert result.url == "https://github.com/owner/repo/pull/42"

    def test_get_existing_returns_none_when_no_pr(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="no open PR for branch",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_existing("feature-branch")

        assert result is None

    def test_get_existing_returns_none_for_empty_list(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_existing("feature-branch")

        assert result is None

    def test_get_existing_handles_invalid_json(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="invalid",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_existing("feature-branch")

        assert result is None


class TestPRManagerGetDiff:
    """Tests for PRManager.get_diff()."""

    def test_get_diff_returns_content(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="+added line\n-old line",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_diff(123, retries=1, delay=1)

        assert "+added line" in result

    def test_get_diff_retries_on_empty_diff(self, mock_runner, mock_logger):
        empty_response = MagicMock(returncode=0, stdout="", stderr="")
        non_empty_response = MagicMock(returncode=0, stdout="+real diff", stderr="")

        mock_runner.run.side_effect = [empty_response, empty_response, non_empty_response]

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_diff(123, retries=3, delay=0)

        assert "+real diff" in result
        assert mock_runner.run.call_count == 3

    def test_get_diff_no_retries_when_content_exists(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="+content",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_diff(123, retries=5, delay=10)

        assert "+content" in result
        assert mock_runner.run.call_count == 1


class TestPRManagerGetDiffForFile:
    """Tests for PRManager.get_diff_for_file()."""

    def test_get_diff_for_specific_file(self, mock_runner, mock_logger):
        full_diff = """diff --git a/src/main.py b/src/main.py
index 1234567..89abcdef 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1 +1,2 @@
+new line
diff --git a/src/utils.py b/src/utils.py
--- a/src/utils.py
+++ b/src/utils.py
@@ -1 +1 @@
-old line
"""
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout=full_diff,
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_diff_for_file(123, "src/main.py")

        assert "src/main.py" in result
        assert "src/utils.py" not in result


class TestPRManagerGetChecks:
    """Tests for PRManager.get_checks()."""

    def test_get_checks_parses_json(self, mock_runner, mock_logger):
        check_data = [
            {"name": "build", "state": "COMPLETED", "conclusion": "SUCCESS", "required": True},
            {"name": "test", "state": "COMPLETED", "conclusion": "FAILURE", "required": True},
        ]
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(check_data),
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)
        result = pr_manager.get_checks(123)

        assert len(result) == 2
        assert result[0]["name"] == "build"
        assert result[1]["conclusion"] == "FAILURE"

    def test_get_checks_raises_on_error(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error",
        )

        pr_manager = PRManager(mock_runner, mock_logger)

        with pytest.raises(Exception, match="gh pr checks failed"):
            pr_manager.get_checks(123)

    def test_get_checks_handles_empty_output(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="",
            stderr="",
        )

        pr_manager = PRManager(mock_runner, mock_logger)

        with pytest.raises(Exception, match="Failed to parse"):
            pr_manager.get_checks(123)


class TestPRManagerMerge:
    """Tests for PRManager.merge()."""

    def test_merge_uses_squash(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        pr_manager = PRManager(mock_runner, mock_logger)
        pr_manager.merge(123)

        call_args = mock_runner.run.call_args[0][0]
        assert "--squash" in call_args
        assert "--auto" in call_args

    def test_merge_raises_on_failure(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="merge conflict",
        )

        pr_manager = PRManager(mock_runner, mock_logger)

        with pytest.raises(Exception, match="merge conflict"):
            pr_manager.merge(123)


class TestPRManagerClose:
    """Tests for PRManager.close()."""

    def test_close_posts_comment_before_closing(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        pr_manager = PRManager(mock_runner, mock_logger)
        pr_manager.close(123, "Not needed")

        assert mock_runner.run.call_count == 2

    def test_close_raises_on_close_failure(self, mock_runner, mock_logger):
        mock_runner.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_runner.run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="already closed"),
        ]

        pr_manager = PRManager(mock_runner, mock_logger)

        with pytest.raises(Exception, match="already closed"):
            pr_manager.close(123, "reason")


class TestPRInfo:
    """Tests for PRInfo dataclass."""

    def test_pr_info_fields(self):
        info = PRInfo(number=42, url="https://github.com/owner/repo/pull/42")

        assert info.number == 42
        assert info.url == "https://github.com/owner/repo/pull/42"
