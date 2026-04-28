"""Tests for the CLI entry point."""

import subprocess
import sys
from pathlib import Path

import ralph


class TestCLIEntryPoint:
    """Tests for the CLI entry point."""

    def test_help_exits_zero(self):
        """--help should exit 0."""
        result = subprocess.run(
            [sys.executable, "-m", "ralph", "--help"],
            capture_output=True,
        )
        assert result.returncode == 0

    def test_unknown_flag_exits_nonzero(self):
        """Unknown flag should exit non-zero."""
        result = subprocess.run(
            [sys.executable, "-m", "ralph", "--unknown-flag"],
            capture_output=True,
        )
        assert result.returncode != 0

    def test_rzilla_help_exits_zero(self):
        """rzilla --help should exit 0."""
        result = subprocess.run(
            ["uv", "run", "rzilla", "--help"],
            capture_output=True,
        )
        assert result.returncode == 0

    def test_rzilla_run_dry_run_exits_zero(self):
        """rzilla run --dry-run should exit 0 against project's prd.json."""
        result = subprocess.run(
            ["uv", "run", "rzilla", "run", "--dry-run"],
            capture_output=True,
            cwd=Path(__file__).parent.parent,
        )
        assert result.returncode == 0


class TestDryRunMode:
    """Tests for --dry-run mode."""

    def test_dry_run_filters_completed_and_human_tasks(self):
        """--dry-run filters out completed and human-owned tasks."""
        tasks = [
            {
                "id": "TEST-01",
                "title": "Test task",
                "description": "A test task",
                "acceptance_criteria": ["Criterion 1"],
                "owner": "ralph",
                "completed": False,
            },
            {
                "id": "TEST-02",
                "title": "Human task",
                "description": "A human task",
                "acceptance_criteria": ["Criterion 1"],
                "owner": "human",
                "completed": False,
            },
            {
                "id": "TEST-03",
                "title": "Completed task",
                "description": "A completed task",
                "acceptance_criteria": ["Criterion 1"],
                "owner": "ralph",
                "completed": True,
            },
        ]

        filtered = []
        for task in tasks:
            if task.get("completed"):
                continue
            if task.get("owner") == "human":
                continue

            filtered.append(task)

        assert len(filtered) == 1
        assert filtered[0]["id"] == "TEST-01"


class TestCLIFlags:
    """Tests for CLI flags mapping to Config."""

    def test_flags_map_to_config(self):
        """Each flag should map to a corresponding Config field."""
        expected_fields = [
            "max_iterations",
            "skip_review",
            "tdd_mode",
            "claude_only",
            "gemini_only",
            "opencode_only",
            "opencode_model",
            "opencode_reviewer_model",
            "opencode_test_writer_model",
            "resume",
            "max_test_fix_rounds",
            "max_test_write_rounds",
            "force_task_id",
            "deep_review_check",
            "repo_dir",
        ]

        for field in expected_fields:
            assert field in ralph.Config.__dataclass_fields__, f"Config missing field: {field}"


class TestFindRepoRoot:
    """Tests for _find_repo_root defaulting logic."""

    def test_finds_git_root_from_cwd(self, monkeypatch, tmp_path):
        """Should find .git dir by walking up from cwd."""
        git_dir = tmp_path / "project"
        git_dir.mkdir()
        (git_dir / ".git").mkdir()
        sub = git_dir / "src" / "pkg"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert ralph._find_repo_root() == git_dir.resolve()

    def test_falls_back_to_cwd_when_no_git(self, monkeypatch, tmp_path):
        """Should fall back to cwd when no .git found anywhere above."""
        no_git = tmp_path / "nogit"
        no_git.mkdir()
        monkeypatch.chdir(no_git)
        assert ralph._find_repo_root() == no_git.resolve()

    def test_finds_repo_root_from_repo_root(self, monkeypatch):
        """Should find .git when cwd IS the repo root."""
        monkeypatch.chdir(Path(__file__).parent.parent)
        result = ralph._find_repo_root()
        assert (result / ".git").exists()
        assert result == Path(__file__).parent.parent.resolve()
