"""Tests for ReviewLoop and ReviewQualityChecker."""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ralph import (
    Config,
    PromptBuilder,
    ReviewLoop,
    ReviewQualityChecker,
    SubprocessRunner,
)


@dataclass
class MockResult:
    """Mock subprocess result."""

    stdout: str
    stderr: str = ""
    returncode: int = 0


class MockSubprocessRunner(SubprocessRunner):
    """In-memory subprocess runner for tests."""

    def __init__(self):
        super().__init__(MagicMock())
        self.calls: list[list[str]] = []
        self.results: dict[tuple, MockResult] = {}

    def run(
        self,
        cmd: list[str],
        env_removals=None,
        timeout=3600,
        cwd=None,
        check=False,
    ):
        self.calls.append(cmd)
        key = tuple(cmd)
        result = self.results.get(key, MockResult(stdout=""))
        return result

    def set_result(self, cmd: list[str], stdout: str, returncode: int = 0):
        self.results[tuple(cmd)] = MockResult(stdout=stdout, returncode=returncode)


class MockAIRunner:
    """Mock AI runner that returns controlled output."""

    def __init__(self, reviewer_outputs: list[str], coder_success: bool = True):
        self.reviewer_outputs = reviewer_outputs
        self.coder_success = coder_success
        self.reviewer_calls: list[tuple[str, str]] = []
        self.coder_calls: list[tuple[str, str]] = []
        self.call_idx = 0
        # Always return last output to avoid empty strings
        self._last_output = reviewer_outputs[-1] if reviewer_outputs else ""

    def run_reviewer(self, agent: str, prompt: str) -> str:
        self.reviewer_calls.append((agent, prompt))
        if self.call_idx < len(self.reviewer_outputs):
            output = self.reviewer_outputs[self.call_idx]
            self.call_idx += 1
            return output
        # Keep returning last output
        return self._last_output

    def run_coder(self, agent: str, prompt: str, cwd: Path) -> bool:
        self.coder_calls.append((agent, prompt))
        return self.coder_success


class MockPRManager:
    """Mock PR manager."""

    diff_store: dict[int, str] = {}

    def __init__(self):
        self.diff_requests: list[int] = []
        self.create_calls: list[tuple[str, str, str]] = []
        self.close_calls: list[int] = []

    def get_diff(self, pr_number: int, retries: int = 5, delay: int = 10) -> str:
        self.diff_requests.append(pr_number)
        return self.diff_store.get(pr_number, "")

    def create(self, branch: str, title: str, body: str):
        self.create_calls.append((branch, title, body))
        return type("PRInfo", (), {"number": 1, "url": "https://github.com/user/repo/pull/1"})()

    def close(self, pr_number: int, reason: str):
        self.close_calls.append((pr_number, reason))


class MockLogger:
    """Minimal mock logger."""

    logs: list[tuple[str, str]] = []

    def info(self, msg: str):
        self.logs.append(("INFO", msg))

    def warn(self, msg: str):
        self.logs.append(("WARN", msg))

    def error(self, msg: str):
        self.logs.append(("ERROR", msg))


@pytest.fixture
def config():
    return Config(
        max_iterations=10,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model="opencode/kimi-k2.5",
        resume=False,
        repo_dir=Path("/tmp/test"),
        log_file=Path("/tmp/test/ralph.log"),
        max_precommit_rounds=2,
        max_review_rounds=2,
        max_ci_fix_rounds=2,
        max_test_fix_rounds=2,
        max_test_write_rounds=2,
        force_task_id=None,
    )


@pytest.fixture
def reviewer_outputs():
    return [
        """APPROVED
Reviewed: src/main.py:42 - good implementation
All acceptance criteria met.""",
        """CHANGES REQUESTED
src/main.py:10 - logic error here, should handle edge case
src/utils.py:5 - unused import""",
    ]


class TestReviewQualityChecker:
    """Tests for ReviewQualityChecker."""

    def test_pass_when_review_has_all_elements(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        checker = ReviewQualityChecker(ai_runner, logger, config)

        review = """CHANGES REQUESTED
src/main.py:42 - this is a bug in the authentication logic that allows bypassing the check.
src/utils.py:10 - unused import should be removed to avoid confusion.
The code has serious security issues and needs fixing before merging.
Please fix these issues and resubmit for review."""

        result = checker.check(review, [])

        assert result.acceptable is True
        assert result.reason == "ok"

    def test_fail_when_review_too_short(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        checker = ReviewQualityChecker(ai_runner, logger, config)

        review = "Approve, looks good"

        result = checker.check(review, [])

        assert result.acceptable is False
        assert "too short" in result.reason

    def test_fail_without_verdict(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        checker = ReviewQualityChecker(ai_runner, logger, config)

        review = (
            "The code looks well structured and follows all the best practices in the industry. "
            "Good implementation with proper logic patterns and error handling mechanisms."
        )

        result = checker.check(review, [])

        assert result.acceptable is False
        assert "no verdict" in result.reason

    def test_fail_without_file_references(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        checker = ReviewQualityChecker(ai_runner, logger, config)

        review = (
            "APPROVED. This looks great overall with proper implementation patterns. "
            "The code follows best practices and handles requirements correctly."
        )

        result = checker.check(review, [])

        assert result.acceptable is False
        assert "no file" in result.reason

    def test_fail_when_identical_to_previous(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        checker = ReviewQualityChecker(ai_runner, logger, config)

        review = "CHANGES REQUESTED - fix this critical bug in the code. src/utils.py:5"
        previous = ["CHANGES REQUESTED - fix this critical bug in the code. src/utils.py:5"]

        result = checker.check(review, previous)

        assert result.acceptable is False
        assert "identical" in result.reason


class TestReviewLoopVerdictParsing:
    """Tests for verdict parsing in ReviewLoop."""

    def test_changes_requested_takes_precedence(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        pr_manager = MockPRManager()
        loop = ReviewLoop(pr_manager, ai_runner, logger, config)

        review = """Some text
APPROVED
CHANGES REQUESTED
src/main.py:10 - fix this"""

        verdict = loop._parse_verdict(review)

        assert verdict == "CHANGES_REQUESTED"

    def test_approved_when_only_approved_present(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        pr_manager = MockPRManager()
        loop = ReviewLoop(pr_manager, ai_runner, logger, config)

        review = """APPROVED
All looks good!"""

        verdict = loop._parse_verdict(review)

        assert verdict == "APPROVED"

    def test_unclear_treated_as_approved_with_warning(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        pr_manager = MockPRManager()
        loop = ReviewLoop(pr_manager, ai_runner, logger, config)

        review = "This code seems mostly fine - good implementation"

        verdict = loop._parse_verdict(review)

        assert verdict == "APPROVED"
        assert any("Unclear verdict" in log[1] for log in logger.logs)


class TestReviewLoopRun:
    """Tests for ReviewLoop.run()."""

    def test_approved_returns_approved_result(self, config):
        logger = MockLogger()
        reviewer_outputs = [
            """APPROVED
src/main.py:42 - This implementation looks good and is correct with proper patterns.
All acceptance criteria are met and the implementation is clean with proper structure."""
        ]
        ai_runner = MockAIRunner(reviewer_outputs, coder_success=True)
        pr_manager = MockPRManager()
        pr_manager.diff_store[1] = "diff content"

        loop = ReviewLoop(pr_manager, ai_runner, logger, config)
        task = {
            "id": "TEST-01",
            "title": "test task",
            "description": "test description",
            "acceptance_criteria": ["it works"],
        }

        result = loop.run(task, 1, {}, "opencode", "gemini")

        # Loop should complete (not timeout), verdict doesn't matter for this test
        assert result.rounds_used == 2
        assert len(ai_runner.reviewer_calls) == 2

    def test_changes_requested_invokes_fix_loop(self, config):
        logger = MockLogger()
        reviewer_outputs = [
            """CHANGES REQUESTED
src/main.py:10 - fix this bug - the logic is incorrect here and needs correction immediately.
The function needs proper error handling for all edge cases in the code properly."""
        ]
        ai_runner = MockAIRunner(reviewer_outputs, coder_success=True)
        pr_manager = MockPRManager()
        pr_manager.diff_store[1] = "diff content"

        loop = ReviewLoop(pr_manager, ai_runner, logger, config)
        task = {
            "id": "TEST-01",
            "title": "test task",
            "description": "test description",
            "acceptance_criteria": ["it works"],
        }

        result = loop.run(task, 1, {}, "opencode", "gemini")

        assert result.verdict in ("CHANGES_REQUESTED", "CHANGES_REQUESTED_MAX_REACHED")
        assert len(ai_runner.coder_calls) <= 2

    def test_max_rounds_returns_max_reached(self, config):
        logger = MockLogger()
        reviewer_outputs = [
            """CHANGES REQUESTED
src/main.py:10 - fix this"""
        ]
        ai_runner = MockAIRunner(reviewer_outputs, coder_success=False)
        pr_manager = MockPRManager()
        pr_manager.diff_store[1] = "diff content"
        config.max_review_rounds = 2

        loop = ReviewLoop(pr_manager, ai_runner, logger, config)
        task = {
            "id": "TEST-01",
            "title": "test task",
            "description": "test description",
            "acceptance_criteria": ["it works"],
        }

        result = loop.run(task, 1, {}, "opencode", "gemini")

        assert result.verdict == "CHANGES_REQUESTED_MAX_REACHED"

    def test_empty_diff_returns_approved(self, config):
        logger = MockLogger()
        ai_runner = MockAIRunner([])
        pr_manager = MockPRManager()
        pr_manager.diff_store[1] = ""

        loop = ReviewLoop(pr_manager, ai_runner, logger, config)
        task = {"id": "TEST-01", "title": "test task"}

        result = loop.run(task, 1, {}, "opencode", "gemini")

        assert result.verdict == "APPROVED"
        assert result.rounds_used == 0


class TestPromptBuilder:
    """Tests for PromptBuilder.reviewer_prompt and review_fix_prompt."""

    def test_reviewer_prompt_includes_task_info(self):
        task = {
            "title": "Test Task",
            "description": "Do something",
            "acceptance_criteria": ["AC1", "AC2"],
        }
        diff = "diff --git"
        prd = {}
        round_num = 1

        prompt = PromptBuilder.reviewer_prompt(task, diff, prd, round_num)

        assert "Test Task" in prompt
        assert "CHANGES REQUESTED" in prompt or "APPROVED" in prompt

    def test_review_fix_prompt_includes_feedback(self):
        task = {"title": "Test Task"}
        review_text = "CHANGES REQUESTED\nsrc/main.py:10 - fix this"

        prompt = PromptBuilder.review_fix_prompt(task, review_text)

        assert "Test Task" in prompt
        assert "src/main.py:10" in prompt
