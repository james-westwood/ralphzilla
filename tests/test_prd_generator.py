import json
from unittest.mock import MagicMock, patch

import pytest

from ralph import (
    GITHUB_ISSUE_PATTERN,
    PrdGenerator,
    PrdValidator,
    RalphError,
    SubprocessRunner,
)


class TestPrdGeneratorGitHubUrlDetection:
    def test_is_github_issue_url_matches(self):
        url = "https://github.com/james-westwood/ralphzilla/issues/123"
        assert GITHUB_ISSUE_PATTERN.search(url) is not None

    def test_is_github_issue_url_does_not_match_plain_text(self):
        text = "Add user authentication feature"
        assert GITHUB_ISSUE_PATTERN.search(text) is None

    def test_is_github_issue_url_does_not_match_pr_url(self):
        pr_url = "https://github.com/james-westwood/ralphzilla/pull/456"
        assert GITHUB_ISSUE_PATTERN.search(pr_url) is None


class TestPrdGeneratorConstructor:
    def test_constructor(self):
        ai_runner = MagicMock()
        task_tracker = MagicMock()
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        assert generator.ai_runner is ai_runner
        assert generator.task_tracker is task_tracker
        assert generator.validator is validator
        assert generator.runner is runner
        assert generator.logger is logger


class TestPrdGeneratorInferNextEpicPrefix:
    def test_infer_next_epic_prefix_empty(self):
        ai_runner = MagicMock()
        task_tracker = MagicMock()
        task_tracker.load.return_value = {"tasks": []}
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        result = generator._infer_next_epic_prefix({"tasks": []})

        assert result == 1

    def test_infer_next_epic_prefix_finds_max(self):
        ai_runner = MagicMock()
        task_tracker = MagicMock()
        task_tracker.load.return_value = {"tasks": []}
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        prd = {
            "tasks": [
                {"id": "M1-01"},
                {"id": "M2-01"},
                {"id": "M3-01"},
                {"id": "M4-01"},
            ]
        }
        result = generator._infer_next_epic_prefix(prd)

        assert result == 5


class TestPrdGeneratorGenerate:
    @patch("ralph.PromptBuilder")
    def test_generate_parses_json_response(self, mock_builder):
        long_desc = (
            "Test description here for validation purposes and must be at least "
            "one hundred characters long to pass validation"
        )
        ai_runner = MagicMock()
        ai_runner.run_reviewer.return_value = (
            f'[{{"title": "Test", "description": "{long_desc}", '
            f'"acceptance_criteria": ["tests/test_file.py"]}}]'
        )

        task_tracker = MagicMock()
        task_tracker.load.return_value = {"tasks": []}
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        mock_builder.prd_generate_prompt.return_value = "Generate tasks"

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        tasks = generator.generate("Add test feature")

        assert len(tasks) == 1
        assert task_tracker.add_task.call_count == 1
        added_task = task_tracker.add_task.call_args_list[0][0][0]
        assert added_task["id"] == "M1-01"

    @patch("ralph.PromptBuilder")
    def test_generate_validates_each_task(self, mock_builder):
        ai_runner = MagicMock()
        ai_runner.run_reviewer.return_value = (
            '[{"title": "Test", "description": "Too short", '
            '"acceptance_criteria": ["tests/test_file.py"]}]'
        )

        task_tracker = MagicMock()
        task_tracker.load.return_value = {"tasks": []}
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        mock_builder.prd_generate_prompt.return_value = "Generate tasks"

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        with pytest.raises(RalphError, match="description too short"):
            generator.generate("Add test feature")

    @patch("ralph.PromptBuilder")
    def test_generate_calls_task_tracker_add_task(self, mock_builder):
        long_desc = (
            "Test description here is properly long and will definitely "
            "pass validation because it is over one hundred characters"
        )
        ai_runner = MagicMock()
        ai_runner.run_reviewer.return_value = (
            f'[{{"title": "Test", "description": "{long_desc}", '
            f'"acceptance_criteria": ["tests/test_file.py"]}}]'
        )

        task_tracker = MagicMock()
        task_tracker.load.return_value = {"tasks": []}
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        mock_builder.prd_generate_prompt.return_value = "Generate tasks"

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        generator.generate("Add test feature")

        assert task_tracker.add_task.call_count == 1


class TestPrdGeneratorGHIssueFetch:
    def test_fetch_issue_body(self):
        ai_runner = MagicMock()
        task_tracker = MagicMock()
        task_tracker.load.return_value = {"tasks": []}
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        runner.run.return_value = MagicMock(
            stdout=json.dumps({"title": "Test Issue", "body": "Test body"}),
            returncode=0,
        )

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        result = generator._fetch_issue_body("https://github.com/owner/repo/issues/123")

        assert "Test Issue" in result
        assert "Test body" in result

    def test_fetch_issue_body_no_body(self):
        ai_runner = MagicMock()
        task_tracker = MagicMock()
        task_tracker.load.return_value = {"tasks": []}
        validator = PrdValidator()
        runner = MagicMock(spec=SubprocessRunner)
        logger = MagicMock()

        runner.run.return_value = MagicMock(
            stdout=json.dumps({"title": "Test Issue", "body": ""}),
            returncode=0,
        )

        generator = PrdGenerator(ai_runner, task_tracker, validator, runner, logger)

        result = generator._fetch_issue_body("https://github.com/owner/repo/issues/123")

        assert result == "Test Issue"
