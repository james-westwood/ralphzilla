from unittest.mock import MagicMock

from ralph import (
    BlockerKind,
    BlockerResult,
    UnblockResult,
    UnblockStrategy,
)


def test_unblock_result_dataclass():
    result = UnblockResult(
        success=True,
        actions_log=["action 1", "action 2"],
    )

    assert result.success is True
    assert len(result.actions_log) == 2
    assert result.actions_log[0] == "action 1"


def test_unblock_result_with_all_fields():
    result = UnblockResult(
        success=True,
        actions_log=["created fix ticket"],
        escalated=True,
        replacement_task_id="M5-01",
        skip_to_next=True,
        alternative_model="gemini",
    )

    assert result.success is True
    assert result.escalated is True
    assert result.replacement_task_id == "M5-01"
    assert result.skip_to_next is True
    assert result.alternative_model == "gemini"


class TestMergeConflictStrategy:
    def test_merge_conflict_strategy_resets_and_reapplies(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")
        branch_manager.verify_ssh_remote = MagicMock()

        pr_manager = MagicMock()
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        ai_runner.run_coder = MagicMock(return_value=True)
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=BlockerKind.MERGE_CONFLICT,
            task_id="M4-01",
            context="merge conflict in ralph.py",
        )
        task = {
            "id": "M4-01",
            "title": "Test Task",
            "description": "Test description",
            "acceptance_criteria": ["Test AC"],
        }

        result = strategy.execute(blocker, task, {})

        assert result.success is True
        assert len(result.actions_log) > 0
        assert any("merge conflict" in log.lower() for log in result.actions_log)
        assert any("reset" in log.lower() for log in result.actions_log)
        assert any(
            "re-apply" in log.lower() or "coder" in log.lower() for log in result.actions_log
        )

    def test_merge_conflict_strategy_fails_gracefully(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.runner.run = MagicMock(side_effect=Exception("git failed"))
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")
        branch_manager.verify_ssh_remote = MagicMock()

        pr_manager = MagicMock()
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=BlockerKind.MERGE_CONFLICT,
            task_id="M4-01",
            context="merge conflict",
        )
        task = {"id": "M4-01", "title": "Test Task", "description": "Desc"}

        result = strategy.execute(blocker, task, {})

        assert result.success is False
        assert result.escalated is True


class TestCIFatalStrategy:
    def test_ci_fatal_creates_fix_ticket_via_backlog_manager(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.runner.run = MagicMock(return_value=MagicMock(stdout="Issue created #42"))
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")

        pr_manager = MagicMock()
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=BlockerKind.CI_FATAL,
            task_id="M4-02",
            context="CI failed: tests failed after 2 fix rounds",
        )
        task = {
            "id": "M4-02",
            "title": "Test CI Task",
            "description": "Test CI description",
            "acceptance_criteria": ["Tests pass"],
        }

        result = strategy.execute(blocker, task, {})

        assert result.success is True
        assert result.skip_to_next is True
        assert len(result.actions_log) > 0
        assert any("ci" in log.lower() for log in result.actions_log)
        assert any("ticket" in log.lower() or "fix" in log.lower() for log in result.actions_log)
        assert any("skip" in log.lower() for log in result.actions_log)

    def test_ci_fatal_handles_ticket_creation_failure(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.runner.run = MagicMock(side_effect=Exception("gh failed"))
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")

        pr_manager = MagicMock()
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=BlockerKind.CI_FATAL,
            task_id="M4-02",
            context="CI failed",
        )
        task = {"id": "M4-02", "title": "Test Task", "description": "Desc"}

        result = strategy.execute(blocker, task, {})

        assert result.success is False
        assert result.skip_to_next is True
        assert result.escalated is True


class TestPRDGuardViolationStrategy:
    def test_prd_guard_rolls_back_and_escalates(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")

        pr_manager = MagicMock()
        pr_manager.close = MagicMock()

        task_tracker = MagicMock()
        task_tracker.add_task = MagicMock()

        ai_runner = MagicMock()
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=BlockerKind.PRD_GUARD_VIOLATION,
            task_id="M4-03",
            context="prd.json was modified by coder",
        )
        task = {
            "id": "M4-03",
            "title": "Test PRD Task",
            "description": "Test PRD guard violation task",
            "acceptance_criteria": ["AC 1"],
            "owner": "ralph",
            "completed": False,
            "depends_on": [],
            "epic": "M4",
            "pr_number": 123,
        }
        prd = {"tasks": [{"id": "M4-01", "title": "Existing Task"}]}

        result = strategy.execute(blocker, task, prd)

        assert result.success is True
        assert result.escalated is True
        assert result.replacement_task_id is not None
        assert len(result.actions_log) > 0
        assert any("prd" in log.lower() or "guard" in log.lower() for log in result.actions_log)
        assert any(
            "rollback" in log.lower() or "close" in log.lower() for log in result.actions_log
        )
        assert any(
            "replacement" in log.lower() or "review" in log.lower() for log in result.actions_log
        )
        task_tracker.add_task.assert_called_once()

    def test_prd_guard_creates_replacement_task(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")

        pr_manager = MagicMock()
        pr_manager.close = MagicMock()

        task_tracker = MagicMock()
        task_tracker.add_task = MagicMock()

        ai_runner = MagicMock()
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=BlockerKind.PRD_GUARD_VIOLATION,
            task_id="M4-03",
            context="prd.json modified",
        )
        task = {
            "id": "M4-03",
            "title": "Original Task",
            "description": "Original description",
            "acceptance_criteria": ["Test AC"],
            "owner": "ralph",
            "completed": False,
            "depends_on": [],
            "epic": "M4",
        }
        prd = {"tasks": []}

        result = strategy.execute(blocker, task, prd)

        assert result.replacement_task_id is not None
        assert "-FIX" in result.replacement_task_id
        task_tracker.add_task.assert_called_once()


class TestReviewerUnavailableStrategy:
    def test_reviewer_unavailable_switches_model(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")

        pr_manager = MagicMock()
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=BlockerKind.REVIEWER_UNAVAILABLE,
            task_id="M4-04",
            context="Reviewer claude returned no output",
        )
        task = {
            "id": "M4-04",
            "title": "Test Reviewer Task",
            "description": "Test reviewer unavailable task",
            "acceptance_criteria": ["AC 1"],
        }

        result = strategy.execute(blocker, task, {})

        assert result.success is True
        assert result.alternative_model is not None
        assert result.alternative_model in ["gemini", "claude", "opencode"]
        assert len(result.actions_log) > 0
        assert any("reviewer" in log.lower() for log in result.actions_log)
        assert any(
            "alternative" in log.lower() or "switch" in log.lower() for log in result.actions_log
        )


class TestUnknownBlockerKind:
    def test_unknown_blocker_kind_escalates(self):
        branch_manager = MagicMock()
        branch_manager.repo_dir = MagicMock()
        branch_manager.runner = MagicMock()
        branch_manager.sanitise_branch_name = MagicMock(return_value="test-task")

        pr_manager = MagicMock()
        task_tracker = MagicMock()
        ai_runner = MagicMock()
        logger = MagicMock()

        strategy = UnblockStrategy(
            branch_manager=branch_manager,
            pr_manager=pr_manager,
            task_tracker=task_tracker,
            ai_runner=ai_runner,
            logger=logger,
        )

        blocker = BlockerResult(
            kind=MagicMock(),  # Unknown kind - not matching any enum
            task_id="M4-05",
            context="Unknown error",
        )
        task = {"id": "M4-05", "title": "Test Task", "description": "Desc"}

        result = strategy.execute(blocker, task, {})

        assert result.success is False
        assert result.escalated is True
