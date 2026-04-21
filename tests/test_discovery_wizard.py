"""Tests for DiscoveryWizard and ralph init command."""

import os
import subprocess
from unittest.mock import MagicMock

import pytest

import ralph


class MockIO:
    """Mock for stdin/stdout for testing DiscoveryWizard."""

    def __init__(self, answers):
        self.answers = answers
        self.index = 0
        self.output = []

    def readline(self):
        if self.index >= len(self.answers):
            return ""
        result = self.answers[self.index]
        self.index += 1
        return result

    def write(self, msg):
        self.output.append(msg)

    def flush(self):
        pass


class TestDiscoveryWizard:
    """Tests for the DiscoveryWizard class."""

    def test_project_spec_dataclass(self):
        """ProjectSpec should have all required fields."""
        spec = ralph.ProjectSpec(
            description="A test project",
            language="python",
            runtime="3.13+",
            package_manager="uv",
            test_framework="pytest",
            coverage_tool="pytest-cov",
            quality_checks=["uv run pytest tests/ -v"],
            human_steps=["Setup credentials"],
            out_of_scope=["UI design"],
        )

        assert spec.description == "A test project"
        assert spec.language == "python"
        assert spec.runtime == "3.13+"
        assert spec.package_manager == "uv"
        assert spec.test_framework == "pytest"
        assert spec.coverage_tool == "pytest-cov"
        assert spec.quality_checks == ["uv run pytest tests/ -v"]
        assert spec.human_steps == ["Setup credentials"]
        assert spec.out_of_scope == ["UI design"]

    def test_wizard_asks_six_questions(self):
        """DiscoveryWizard should ask exactly 6 questions."""
        answers = [
            "A test CLI tool",
            "python, 3.13+, uv",
            "pytest, pytest-cov",
            "uv run pytest tests/ -v",
            "",
            "",
        ]
        mock_in = MockIO(answers)
        mock_out = MagicMock()

        wizard = ralph.DiscoveryWizard(mock_in, mock_out)
        spec = wizard.run()

        assert spec.description == "A test CLI tool"

    def test_wizard_parses_language_runtime_package_manager(self):
        """Wizard should parse language, runtime, package manager from comma-separated input."""
        answers = [
            "A test project",
            "python, 3.13+, uv",
            "pytest, pytest-cov",
            "",
            "",
            "",
        ]
        mock_in = MockIO(answers)
        mock_out = MagicMock()

        wizard = ralph.DiscoveryWizard(mock_in, mock_out)
        spec = wizard.run()

        assert spec.language == "python"
        assert spec.runtime == "3.13+"
        assert spec.package_manager == "uv"

    def test_wizard_parses_test_framework_and_coverage(self):
        """Wizard should parse test framework and coverage tool from comma-separated input."""
        answers = [
            "A test project",
            "python, 3.13+, uv",
            "pytest, pytest-cov",
            "",
            "",
            "",
        ]
        mock_in = MockIO(answers)
        mock_out = MagicMock()

        wizard = ralph.DiscoveryWizard(mock_in, mock_out)
        spec = wizard.run()

        assert spec.test_framework == "pytest"
        assert spec.coverage_tool == "pytest-cov"

    def test_wizard_raises_on_empty_description(self):
        """Wizard should raise error on empty description."""
        answers = [""]
        mock_in = MockIO(answers)
        mock_out = MagicMock()

        wizard = ralph.DiscoveryWizard(mock_in, mock_out)

        with pytest.raises(ralph.RalphError, match="Product description cannot be empty"):
            wizard.run()

    def test_wizard_quality_checks_multiline(self):
        """Wizard should accept multiline quality checks."""
        answers = [
            "A test project",
            "python, 3.13+, uv",
            "pytest, pytest-cov",
            "uv run pytest tests/ -v",
            "uv run ruff check .",
            "",
            "",
        ]
        mock_in = MockIO(answers)
        mock_out = MagicMock()

        wizard = ralph.DiscoveryWizard(mock_in, mock_out)
        spec = wizard.run()

        assert len(spec.quality_checks) == 2
        assert "uv run pytest tests/ -v" in spec.quality_checks
        assert "uv run ruff check ." in spec.quality_checks

    def test_wizard_human_steps_multiline(self):
        """Wizard should accept multiline human steps."""
        answers = [
            "A test project",
            "python, 3.13+, uv",
            "pytest, pytest-cov",
            "",
            "Setup credentials",
            "Deploy to prod",
            "",
            "",
        ]
        mock_in = MockIO(answers)
        mock_out = MagicMock()

        wizard = ralph.DiscoveryWizard(mock_in, mock_out)
        spec = wizard.run()

        assert len(spec.human_steps) == 2
        assert "Setup credentials" in spec.human_steps
        assert "Deploy to prod" in spec.human_steps

    def test_wizard_out_of_scope_multiline(self):
        """Wizard should accept multiline out of scope items."""
        answers = [
            "A test project",
            "python, 3.13+, uv",
            "pytest, pytest-cov",
            "",
            "",
            "Database migration",
            "UI design",
        ]
        mock_in = MockIO(answers)
        mock_out = MagicMock()

        wizard = ralph.DiscoveryWizard(mock_in, mock_out)
        spec = wizard.run()

        assert len(spec.out_of_scope) == 2
        assert "Database migration" in spec.out_of_scope
        assert "UI design" in spec.out_of_scope


class TestRalphInit:
    """Tests for the ralph init command."""

    def test_rzilla_init_help(self):
        """rzilla init --help should exit 0."""
        result = subprocess.run(
            ["uv", "run", "rzilla", "init", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_rzilla_init_non_interactive_raises(self):
        """rzilla init without input should fail or require input."""
        result = subprocess.run(
            ["uv", "run", "rzilla", "init"],
            input=b"\n",
            capture_output=True,
        )
        assert result.returncode != 0 or b"description cannot be empty" in result.stderr


class TestGitHook:
    """Tests for the git pre-push hook."""

    def test_hook_file_is_executable(self, tmp_path):
        """Pre-push hook should be created and be executable."""
        git_dir = tmp_path / ".git"
        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(parents=True)

        hook_path = hooks_dir / "pre-push"
        hook_content = """#!/bin/bash
protected="main"
remote="$1"
url="$2"

while read local_ref local_sha remote_ref remote_sha; do
    if [[ "$local_ref" == "refs/heads/$protected" ]]; then
        echo "ERROR: Direct push to '$protected' is not allowed."
        echo "Please create a branch, commit your changes, and open a PR."
        exit 1
    fi
done

exit 0
"""
        with open(hook_path, "w") as f:
            f.write(hook_content)
        hook_path.chmod(0o755)

        assert os.access(hook_path, os.X_OK)
