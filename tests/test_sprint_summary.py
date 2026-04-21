import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ralph import (
    Config,
    Orchestrator,
    RalphLogger,
    TaskExecutionResult,
)


@pytest.fixture
def tmp_repo_dir(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    prd_path = repo_dir / "prd.json"
    prd_data = {
        "tasks": [
            {
                "id": "M1-01",
                "title": "Test Task 1",
                "description": "A test task with enough description to pass validation rules",
                "acceptance_criteria": ["Test AC 1", "Test AC 2"],
                "owner": "ralph",
                "completed": True,
            },
            {
                "id": "M1-02",
                "title": "Test Task 2",
                "description": "Another test task with enough description to pass validation rules",
                "acceptance_criteria": ["Test AC"],
                "owner": "ralph",
                "completed": True,
            },
            {
                "id": "M1-03",
                "title": "Test Task 3",
                "description": "Third test task with enough description to pass validation rules",
                "acceptance_criteria": ["Test AC"],
                "owner": "ralph",
                "completed": False,
            },
        ]
    }
    prd_path.write_text(json.dumps(prd_data), encoding="utf-8")

    progress_path = repo_dir / "progress.txt"
    progress_path.write_text("", encoding="utf-8")

    return repo_dir


@pytest.fixture
def config(tmp_repo_dir):
    return Config(
        max_iterations=10,
        skip_review=False,
        tdd_mode=False,
        model_mode="random",
        opencode_model="opencode/kimi-k2.5",
        resume=False,
        repo_dir=tmp_repo_dir,
        log_file=tmp_repo_dir / "ralph.log",
        max_precommit_rounds=2,
        max_review_rounds=2,
        max_ci_fix_rounds=2,
        max_test_fix_rounds=2,
        max_test_write_rounds=2,
        force_task_id=None,
    )


@pytest.fixture
def logger(tmp_repo_dir):
    return RalphLogger(tmp_repo_dir / "ralph.log")


@pytest.fixture
def orchestrator(config, logger):
    return Orchestrator(config, logger)


def test_summary_file_created_with_correct_name(orchestrator, tmp_repo_dir, logger):
    orchestrator._sprint_start_time = datetime.now()
    orchestrator._task_results = [
        TaskExecutionResult(
            task_id="M1-01",
            title="Test Task 1",
            pr_number=1,
            ci_passed=True,
            ci_rounds_used=1,
            escalated=False,
            fatal_error_type=None,
            fatal_error_reason=None,
        ),
    ]
    orchestrator._iterations_consumed = 1

    with patch.object(orchestrator.task_tracker, "load") as mock_load:
        mock_load.return_value = {
            "tasks": [
                {"id": "M1-01", "title": "Test Task 1", "completed": True, "pr_number": 1},
            ]
        }

        with patch.object(orchestrator.loop_supervisor, "verify_clean_exit") as mock_verify:
            mock_verify.return_value = MagicMock(
                clean=True, has_sprint_complete=True, has_progress_update=True, no_traceback=True
            )

            with patch.object(orchestrator.loop_supervisor, "record_run"):
                orchestrator._finalize_run()

    summary_files = list(tmp_repo_dir.glob("ralph-summary-*.md"))
    assert len(summary_files) == 1
    assert summary_files[0].name.startswith("ralph-summary-")
    assert summary_files[0].name.endswith(".md")
    timestamp_part = summary_files[0].name.replace("ralph-summary-", "").replace(".md", "")
    datetime.fromisoformat(timestamp_part)


def test_frontmatter_parseable_yaml(orchestrator, tmp_repo_dir):
    orchestrator._sprint_start_time = datetime(2026, 4, 21, 10, 0, 0)
    orchestrator._task_results = []
    orchestrator._iterations_consumed = 0

    with patch.object(orchestrator.task_tracker, "load") as mock_load:
        mock_load.return_value = {"tasks": []}

        with patch.object(orchestrator.loop_supervisor, "verify_clean_exit") as mock_verify:
            mock_verify.return_value = MagicMock(
                clean=True, has_sprint_complete=True, has_progress_update=True, no_traceback=True
            )

            with patch.object(orchestrator.loop_supervisor, "record_run"):
                orchestrator._finalize_run()

    summary_files = list(tmp_repo_dir.glob("ralph-summary-*.md"))
    content = summary_files[0].read_text(encoding="utf-8")

    parts = content.split("---")
    assert len(parts) >= 3

    frontmatter_text = parts[1].strip()
    data = yaml.safe_load(frontmatter_text)

    assert "sprint_start" in data
    assert "sprint_end" in data
    assert "tasks_completed_count" in data
    assert "total_tasks" in data
    assert "fatal_errors_count" in data
    assert "readiness_score_percent" in data

    assert data["tasks_completed_count"] == 0
    assert data["total_tasks"] == 0
    assert data["fatal_errors_count"] == 0
    assert data["readiness_score_percent"] == 100


def test_includes_all_completed_tasks(orchestrator, tmp_repo_dir):
    orchestrator._sprint_start_time = datetime.now()
    orchestrator._task_results = [
        TaskExecutionResult(
            task_id="M1-01",
            title="Task One",
            pr_number=101,
            ci_passed=True,
            ci_rounds_used=1,
            escalated=False,
            fatal_error_type=None,
            fatal_error_reason=None,
        ),
        TaskExecutionResult(
            task_id="M1-02",
            title="Task Two",
            pr_number=102,
            ci_passed=True,
            ci_rounds_used=2,
            escalated=False,
            fatal_error_type=None,
            fatal_error_reason=None,
        ),
    ]
    orchestrator._iterations_consumed = 2

    with patch.object(orchestrator.task_tracker, "load") as mock_load:
        mock_load.return_value = {
            "tasks": [
                {"id": "M1-01", "title": "Task One", "completed": True, "pr_number": 101},
                {"id": "M1-02", "title": "Task Two", "completed": True, "pr_number": 102},
            ]
        }

        with patch.object(orchestrator.loop_supervisor, "verify_clean_exit") as mock_verify:
            mock_verify.return_value = MagicMock(
                clean=True, has_sprint_complete=True, has_progress_update=True, no_traceback=True
            )

            with patch.object(orchestrator.loop_supervisor, "record_run"):
                orchestrator._finalize_run()

    summary_files = list(tmp_repo_dir.glob("ralph-summary-*.md"))
    content = summary_files[0].read_text(encoding="utf-8")

    assert "M1-01" in content
    assert "Task One" in content
    assert "#101" in content
    assert "M1-02" in content
    assert "Task Two" in content
    assert "#102" in content

    assert "Tasks Completed" in content


def test_includes_ci_results_per_task(orchestrator, tmp_repo_dir):
    orchestrator._sprint_start_time = datetime.now()
    orchestrator._task_results = [
        TaskExecutionResult(
            task_id="M1-01",
            title="Task One",
            pr_number=1,
            ci_passed=True,
            ci_rounds_used=1,
            escalated=False,
            fatal_error_type=None,
            fatal_error_reason=None,
        ),
        TaskExecutionResult(
            task_id="M1-02",
            title="Task Two",
            pr_number=2,
            ci_passed=False,
            ci_rounds_used=3,
            escalated=True,
            fatal_error_type="CIFailedFatal",
            fatal_error_reason="CI still failing",
        ),
    ]
    orchestrator._iterations_consumed = 2

    with patch.object(orchestrator.task_tracker, "load") as mock_load:
        mock_load.return_value = {
            "tasks": [
                {"id": "M1-01", "title": "Task One", "completed": True, "pr_number": 1},
                {"id": "M1-02", "title": "Task Two", "completed": False, "pr_number": 2},
            ]
        }

        with patch.object(orchestrator.loop_supervisor, "verify_clean_exit") as mock_verify:
            mock_verify.return_value = MagicMock(
                clean=True, has_sprint_complete=True, has_progress_update=True, no_traceback=True
            )

            with patch.object(orchestrator.loop_supervisor, "record_run"):
                orchestrator._finalize_run()

    summary_files = list(tmp_repo_dir.glob("ralph-summary-*.md"))
    content = summary_files[0].read_text(encoding="utf-8")

    assert "CI Results" in content
    assert "M1-01" in content
    assert "PASSED" in content
    assert "1" in content
    assert "M1-02" in content
    assert "FAILED" in content
    assert "3" in content


def test_includes_escalation_section(orchestrator, tmp_repo_dir):
    orchestrator._sprint_start_time = datetime.now()
    orchestrator._task_results = [
        TaskExecutionResult(
            task_id="M1-01",
            title="Task One",
            pr_number=1,
            ci_passed=True,
            ci_rounds_used=1,
            escalated=False,
            fatal_error_type=None,
            fatal_error_reason=None,
        ),
        TaskExecutionResult(
            task_id="M1-02",
            title="Task Two",
            pr_number=2,
            ci_passed=False,
            ci_rounds_used=2,
            escalated=True,
            fatal_error_type="CIFailedFatal",
            fatal_error_reason="CI failed after max retries",
        ),
        TaskExecutionResult(
            task_id="M1-03",
            title="Task Three",
            pr_number=3,
            ci_passed=False,
            ci_rounds_used=0,
            escalated=True,
            fatal_error_type="BranchSyncError",
            fatal_error_reason="Could not sync with main",
        ),
    ]
    orchestrator._iterations_consumed = 3

    with patch.object(orchestrator.task_tracker, "load") as mock_load:
        mock_load.return_value = {
            "tasks": [
                {"id": "M1-01", "title": "Task One", "completed": True, "pr_number": 1},
                {"id": "M1-02", "title": "Task Two", "completed": False, "pr_number": 2},
                {"id": "M1-03", "title": "Task Three", "completed": False, "pr_number": 3},
            ]
        }

        with patch.object(orchestrator.loop_supervisor, "verify_clean_exit") as mock_verify:
            mock_verify.return_value = MagicMock(
                clean=True, has_sprint_complete=True, has_progress_update=True, no_traceback=True
            )

            with patch.object(orchestrator.loop_supervisor, "record_run"):
                orchestrator._finalize_run()

    summary_files = list(tmp_repo_dir.glob("ralph-summary-*.md"))
    content = summary_files[0].read_text(encoding="utf-8")

    assert "Escalations" in content
    assert "CIFailedFatal" in content
    assert "M1-02" in content
    assert "BranchSyncError" in content
    assert "M1-03" in content
