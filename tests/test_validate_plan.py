from unittest.mock import MagicMock

from ralph import Config, PlanChecker


class TestValidatePlanFlag:
    """Tests for the --validate-plan CLI flag and PlanChecker AI validation."""

    def test_flag_triggers_ai_validation(self):
        """When ai_check=True, PlanChecker.run() should call AIRunner."""
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        ai_runner.run_reviewer.return_value = "No issues found."

        prd = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Test Task",
                    "description": "This is a valid task description that is definitely longer "
                    "than one hundred characters to satisfy the prd validator rule.",
                    "acceptance_criteria": ["Must update tests/test_module.py"],
                    "owner": "ralph",
                    "completed": False,
                }
            ]
        }

        checker = PlanChecker(task_tracker, ai_runner, logger)
        result = checker.run(prd, ai_check=True)

        ai_runner.run_reviewer.assert_called_once()
        assert result.tasks_checked == 1

    def test_warn_format_parsed_correctly(self):
        """PlanChecker should parse [WARN] task_id: reason format correctly."""
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        ai_runner.run_reviewer.return_value = """
Some introductory text.
[WARN] T1: acceptance criteria is too vague - "it works correctly" is not measurable
[WARN] T2: task has multiple distinct deliverables - should be split
More context here.
"""

        prd = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Task 1",
                    "description": "Task 1 description with enough characters "
                    "to meet the minimum length requirement for valid tasks in ralph.",
                    "acceptance_criteria": ["Must update tests/test_module.py"],
                    "owner": "ralph",
                    "completed": False,
                },
                {
                    "id": "T2",
                    "title": "Task 2",
                    "description": "Task 2 description with enough characters "
                    "to meet the minimum length requirement for valid tasks in ralph.",
                    "acceptance_criteria": ["Must update tests/test_module.py"],
                    "owner": "ralph",
                    "completed": False,
                },
            ]
        }

        checker = PlanChecker(task_tracker, ai_runner, logger)
        result = checker.run(prd, ai_check=True)

        assert len(result.warnings) == 2
        assert "T1: acceptance criteria is too vague" in result.warnings[0]
        assert "T2: task has multiple distinct deliverables" in result.warnings[1]

    def test_warnings_logged_but_do_not_block(self):
        """Warnings should not raise PlanInvalidError or block execution."""
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        ai_runner.run_reviewer.return_value = "[WARN] T1: vague acceptance criteria"

        prd = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Test Task",
                    "description": "This is a valid task description that is definitely longer "
                    "than one hundred characters to satisfy the prd validator rule.",
                    "acceptance_criteria": ["Must update tests/test_module.py"],
                    "owner": "ralph",
                    "completed": False,
                }
            ]
        }

        checker = PlanChecker(task_tracker, ai_runner, logger)
        result = checker.run(prd, ai_check=True)

        assert result.valid is True
        assert len(result.warnings) == 1
        assert "T1" in result.warnings[0]
        assert "vague acceptance criteria" in result.warnings[0]

    def test_no_ai_call_when_flag_false(self):
        """When ai_check=False, AIRunner should NOT be called."""
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        prd = {
            "tasks": [
                {
                    "id": "T1",
                    "title": "Test Task",
                    "description": "This is a valid task description that is definitely longer "
                    "than one hundred characters to satisfy the prd validator rule.",
                    "acceptance_criteria": ["Must update tests/test_module.py"],
                    "owner": "ralph",
                    "completed": False,
                }
            ]
        }

        checker = PlanChecker(task_tracker, ai_runner, logger)
        result = checker.run(prd, ai_check=False)

        ai_runner.run_reviewer.assert_not_called()
        assert result.valid is True
        assert result.warnings == []


class TestConfigValidatePlan:
    """Tests for Config.validate_plan field."""

    def test_validate_plan_default_false(self):
        """validate_plan should default to False."""
        config = Config(
            max_iterations=1,
            skip_review=False,
            tdd_mode=False,
            model_mode="random",
            opencode_model="opencode/kimi-k2.5",
            resume=False,
            repo_dir=MagicMock(),
            log_file=MagicMock(),
            max_precommit_rounds=2,
            max_review_rounds=2,
            max_ci_fix_rounds=2,
            max_test_fix_rounds=2,
            max_test_write_rounds=2,
            force_task_id=None,
        )
        assert config.validate_plan is False

    def test_validate_plan_can_be_set_true(self):
        """validate_plan can be explicitly set to True."""
        config = Config(
            max_iterations=1,
            skip_review=False,
            tdd_mode=False,
            model_mode="random",
            opencode_model="opencode/kimi-k2.5",
            resume=False,
            repo_dir=MagicMock(),
            log_file=MagicMock(),
            max_precommit_rounds=2,
            max_review_rounds=2,
            max_ci_fix_rounds=2,
            max_test_fix_rounds=2,
            max_test_write_rounds=2,
            force_task_id=None,
            validate_plan=True,
        )
        assert config.validate_plan is True


class TestPlanCheckResultWarnings:
    """Tests for PlanCheckResult warnings field."""

    def test_plan_check_result_has_warnings_field(self):
        """PlanCheckResult should have warnings field."""
        from ralph import PlanCheckResult

        result = PlanCheckResult(
            valid=True,
            errors=[],
            warnings=["T1: vague AC"],
            tasks_checked=1,
            decompositions=0,
        )
        assert result.warnings == ["T1: vague AC"]

    def test_plan_check_result_warnings_default_empty(self):
        """warnings should default to empty list."""
        from ralph import PlanCheckResult

        result = PlanCheckResult(
            valid=True,
            errors=[],
            warnings=[],
            tasks_checked=1,
            decompositions=0,
        )
        assert result.warnings == []
