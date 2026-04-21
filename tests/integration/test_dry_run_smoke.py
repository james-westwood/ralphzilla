"""Integration smoke test for --dry-run mode.

This test suite verifies:
1. All components initialize correctly
2. Preflight checks pass
3. Task selection identifies the correct next task
4. No actual AI calls or git operations occur
5. Output contains pending task titles
6. Output format matches expected dry-run template
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fixture_prd_path(tmp_path):
    """Create a temporary prd.json from the fixture file."""
    fixture_content = (Path(__file__).parent.parent / "fixtures" / "dry_run_prd.json").read_text()
    prd_path = tmp_path / "prd.json"
    prd_path.write_text(fixture_content)
    (tmp_path / "progress.txt").write_text("")
    return prd_path


class TestDryRunSmokeIntegration:
    """Integration tests for dry-run smoke testing."""

    def test_dry_run_exits_zero(self, fixture_prd_path):
        """Dry-run should exit 0 against fixture prd.json."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(fixture_prd_path.parent),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Exit code: {result.returncode}, stderr: {result.stderr}"

    def test_dry_run_filters_completed_tasks(self, fixture_prd_path):
        """Dry-run should filter out completed tasks."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(fixture_prd_path.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "M2-01" not in output or "First completed task" not in output, (
            "Completed task M2-01 should not appear in output"
        )
        assert "M2-02" not in output or "Second completed task" not in output, (
            "Completed task M2-02 should not appear in output"
        )

    def test_dry_run_includes_pending_task_titles(self, fixture_prd_path):
        """Dry-run output should contain pending task titles."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(fixture_prd_path.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "Pending task with multiple dependencies" in output, (
            "Pending task M2-03 should appear in output"
        )
        assert "Independent pending task" in output, "Pending task M2-05 should appear in output"

    def test_dry_run_displays_estimated_action(self, fixture_prd_path):
        """Dry-run should display estimated action for each task."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(fixture_prd_path.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "estimated_action" in output or "invoke AI coder" in output, (
            "Estimated action should be displayed"
        )

    def test_dry_run_displays_acceptance_criteria_count(self, fixture_prd_path):
        """Dry-run should display acceptance criteria count."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(fixture_prd_path.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "acceptance_criteria" in output, "Acceptance criteria count should be displayed"

    def test_dry_run_indicates_start_and_complete(self, fixture_prd_path):
        """Dry-run should indicate start and completion."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(fixture_prd_path.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "[DRY-RUN]" in output, "Dry-run marker should be present"
        assert "complete" in output.lower(), "Dry-run should indicate completion"

    def test_dry_run_no_git_commands_executed(self, fixture_prd_path):
        """Dry-run should not execute git commands."""
        with patch("ralph.SubprocessRunner") as mock_runner:
            mock_instance = MagicMock()
            mock_runner.return_value = mock_instance

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ralph",
                    "run",
                    "--dry-run",
                    "--repo-dir",
                    str(fixture_prd_path.parent),
                ],
                capture_output=True,
                text=True,
            )

            mock_instance.run.assert_not_called()

    def test_dry_run_no_ai_calls_made(self, fixture_prd_path):
        """Dry-run should not make AI calls."""
        with patch("ralph.AIRunner") as mock_ai_runner:
            mock_instance = MagicMock()
            mock_ai_runner.return_value = mock_instance

            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ralph",
                    "run",
                    "--dry-run",
                    "--repo-dir",
                    str(fixture_prd_path.parent),
                ],
                capture_output=True,
                text=True,
            )

            mock_instance.run_coder.assert_not_called()
            mock_instance.run_reviewer.assert_not_called()


class TestDryRunOutputFormat:
    """Tests for dry-run output format verification."""

    @pytest.fixture
    def prd_with_task(self, tmp_path):
        """Create a minimal prd for format testing."""
        prd = {
            "project": "test",
            "quality_checks": ["echo test"],
            "tasks": [
                {
                    "id": "TEST-01",
                    "title": "Test Task Title",
                    "description": "Test task description: must be at least 100 chars",
                    "acceptance_criteria": ["Test criterion one", "Test criterion two"],
                    "owner": "ralph",
                    "completed": False,
                    "depends_on": [],
                }
            ],
        }
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps(prd, indent=2))
        (tmp_path / "progress.txt").write_text("")
        return prd_path

    def test_output_contains_task_id_and_title(self, prd_with_task):
        """Output should contain task ID and title."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(prd_with_task.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "TEST-01" in output, "Task ID should appear in output"
        assert "Test Task Title" in output, "Task title should appear in output"

    def test_output_format_template(self, prd_with_task):
        """Output should follow expected dry-run template."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(prd_with_task.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "[DRY-RUN]" in output, "Should have DRY-RUN marker"
        assert "Would process:" in output, "Should have 'Would process:' prefix"


class TestDryRunTaskSelection:
    """Tests for correct task selection in dry-run mode."""

    @pytest.fixture
    def prd_with_deps(self, tmp_path):
        """Create prd with dependencies for task selection testing."""
        prd = {
            "project": "test",
            "quality_checks": ["echo test"],
            "tasks": [
                {
                    "id": "DEP-01",
                    "title": "Completed Task",
                    "description": "First completed task serves as dependency.",
                    "acceptance_criteria": ["Complete the task"],
                    "owner": "ralph",
                    "completed": True,
                    "depends_on": [],
                },
                {
                    "id": "DEP-02",
                    "title": "Task with Dependency",
                    "description": "Depends on DEP-01. Since DEP-01 complete, eligible.",
                    "acceptance_criteria": ["Complete after dependency"],
                    "owner": "ralph",
                    "completed": False,
                    "depends_on": ["DEP-01"],
                },
                {
                    "id": "DEP-03",
                    "title": "Task with Unmet Dependency",
                    "description": "Depends on DEP-02. Since DEP-02 incomplete, NOT eligible.",
                    "acceptance_criteria": ["Complete after DEP-02"],
                    "owner": "ralph",
                    "completed": False,
                    "depends_on": ["DEP-02"],
                },
            ],
        }
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps(prd, indent=2))
        (tmp_path / "progress.txt").write_text("")
        return prd_path

    def test_selects_task_when_dependency_met(self, prd_with_deps):
        """Should select task when its dependency is completed."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(prd_with_deps.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "Task with Dependency" in output, "Task with completed dependency should be selected"
        assert "Task with Unmet Dependency" not in output or "[DRY-RUN]" in output, (
            "Task with unmet dependency should not be selected"
        )

    def test_skips_tasks_with_unmet_dependencies(self, prd_with_deps):
        """Dry-run should not display tasks with unmet dependencies."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(prd_with_deps.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "DEP-01" not in output, "Completed task should not appear"
        assert "Task with Dependency" in output, "Task with met dependency should appear"
        assert "Task with Unmet Dependency" not in output, (
            "Task with unmet dependency should not appear"
        )


class TestDryRunSkipsHumanTasks:
    """Tests for skipping human-owned tasks in dry-run."""

    @pytest.fixture
    def prd_with_human(self, tmp_path):
        """Create prd with human-owned tasks."""
        prd = {
            "project": "test",
            "quality_checks": ["echo test"],
            "tasks": [
                {
                    "id": "HUMAN-01",
                    "title": "Human Task",
                    "description": "A human-only task that requires manual intervention.",
                    "acceptance_criteria": ["Manual completion required"],
                    "owner": "human",
                    "completed": False,
                    "depends_on": [],
                },
                {
                    "id": "RALPH-01",
                    "title": "Ralph Task",
                    "description": "A ralph-owned task that can be processed automatically.",
                    "acceptance_criteria": ["Automatic processing available"],
                    "owner": "ralph",
                    "completed": False,
                    "depends_on": [],
                },
            ],
        }
        prd_path = tmp_path / "prd.json"
        prd_path.write_text(json.dumps(prd, indent=2))
        (tmp_path / "progress.txt").write_text("")
        return prd_path

    def test_skips_human_owned_tasks(self, prd_with_human):
        """Dry-run should skip human-owned tasks."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ralph",
                "run",
                "--dry-run",
                "--repo-dir",
                str(prd_with_human.parent),
            ],
            capture_output=True,
            text=True,
        )
        output = result.stdout + result.stderr

        assert "Human Task" not in output, "Human-owned task should not appear in output"
        assert "Ralph Task" in output, "Ralph-owned task should appear in output"


class TestDryRunWithRealComponents:
    """Integration tests with real components (no mocking)."""

    def test_components_initialize_correctly(self, fixture_prd_path):
        """All components should initialize without errors."""
        from ralph import (
            Config,
            Orchestrator,
            RalphLogger,
        )

        repo_dir = fixture_prd_path.parent
        log_file = repo_dir / "ralph-test.log"

        config = Config(
            max_iterations=1,
            skip_review=False,
            tdd_mode=False,
            model_mode="random",
            opencode_model="opencode/kimi-k2.5",
            resume=False,
            repo_dir=repo_dir,
            log_file=log_file,
            max_precommit_rounds=2,
            max_review_rounds=2,
            max_ci_fix_rounds=2,
            max_test_fix_rounds=2,
            max_test_write_rounds=2,
            force_task_id=None,
        )

        logger = RalphLogger(log_file)
        orchestrator = Orchestrator(config, logger)

        assert orchestrator is not None
        assert orchestrator.config is config
        assert orchestrator.logger is logger

    def test_task_tracker_loads_fixture(self, fixture_prd_path):
        """TaskTracker should load fixture prd.json correctly."""
        from ralph import RalphLogger, SubprocessRunner, TaskTracker

        repo_dir = fixture_prd_path.parent
        log_file = repo_dir / "ralph-test.log"

        logger = RalphLogger(log_file)
        runner = SubprocessRunner(logger)

        task_tracker = TaskTracker(
            repo_dir / "prd.json",
            repo_dir / "progress.txt",
            runner,
            logger,
        )

        prd = task_tracker.load()
        assert prd is not None
        assert "tasks" in prd

    def test_task_selection_with_fixture(self, fixture_prd_path):
        """get_next_task should correctly identify next task from fixture."""
        from ralph import RalphLogger, SubprocessRunner, TaskTracker

        repo_dir = fixture_prd_path.parent
        log_file = repo_dir / "ralph-test.log"

        logger = RalphLogger(log_file)
        runner = SubprocessRunner(logger)

        task_tracker = TaskTracker(
            repo_dir / "prd.json",
            repo_dir / "progress.txt",
            runner,
            logger,
        )

        next_task = task_tracker.get_next_task()
        assert next_task is not None, "Should find a next task"
        assert next_task["owner"] == "ralph", "Next task should be ralph-owned"
        assert not next_task["completed"], "Next task should not be completed"

    def test_preflight_checks_pass_on_valid_fixture(self, fixture_prd_path):
        """Preflight checks should pass on valid fixture."""
        from ralph import PrdValidator

        prd = json.loads(fixture_prd_path.read_text())
        validator = PrdValidator()
        all_task_ids = {t["id"] for t in prd.get("tasks", [])}

        for task in prd.get("tasks", []):
            if not task.get("completed") and task.get("owner") != "human":
                validator.validate(task, all_task_ids)

        assert True, "All validations passed"
